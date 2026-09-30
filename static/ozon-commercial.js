(function () {
  'use strict';
  const host = document.getElementById('ozon-commercial-app');
  const boot = document.getElementById('oc-bootstrap');
  if (!host || !boot || !window.Vue || !window.mcatShared) return;
  const config = JSON.parse(boot.textContent);
  const initialListingId = new URLSearchParams(location.search).get('listing_id');
  const requests = new Set();
  const statuses = {pending_review:'Ждёт решения', approved:'Подтверждено', applying:'Проверяем Ozon',
    applied:'Применено', rejected:'Отклонено', failed:'Изменение отклонено', conflict:'Данные изменились',
    uncertain:'Результат требует сверки', cancelled:'Отменено'};
  const clone = value => JSON.parse(JSON.stringify(value));
  const initial = config.filters || {};
  const app = Vue.createApp({
    components: {'ozon-prices': window.mcatShared.ozonPrices},
    data: () => ({mode:config.mode, base:config.base, catalog:config.catalog, writeEnabled:config.writeEnabled,
      statuses, accounts:[], warehouses:[], items:[], proposal:null, loading:false, busy:false, error:'', notice:'',
      sessionEnded:false, stopped:false, actionBlocked:false, confirmed:false, rejectNote:'', errorQuarantine:null,
      filters:{account_id:String(initial.account_id || ''), proposal_kind:initial.proposal_kind || '', status:initial.status || ''},
      observedFilters:{}, pagination:{page:1,pages:0,total:0}, selected:[], failedImages:{},
      listRevision:0, searchRevision:0, contextRevision:0, timer:null, pollDelay:5000, polls:0,
      createAccount:'', query:'', products:[], searched:false, searching:false, productPage:1, productPages:0,
      context:null, contextLoading:false, createOpen:false, formError:'', formBlocked:false,
      kind:'price', amount:'', warehouseId:'', allowDecrease:false, allowLarge:false, guardrailNote:'',
      warehouseRefreshes:{}, stockRefresh:null, stockRefreshListing:null,
      refreshError:'', formRefreshError:'', refreshTimer:null, refreshRevisions:{}, refreshLoaded:new Set(),
      batchReview:[], batchConfirmed:false, batchError:'', batchBlocked:false, batchErrorQuarantine:null,
    }),
    computed: {
      contextListing() { return this.context ? {price_summary:{values:this.context.price,currency:this.context.currency},prices_synced_at:this.context.prices_synced_at} : null; },
      catalogUrl() { return this.catalog + '?marketplace=ozon' + (this.observedFilters.account_id ? '&account_id=' + this.observedFilters.account_id : ''); },
      hasFilters() { return Boolean(this.filters.proposal_kind || this.filters.status); },
      batchMode() { return this.writeEnabled && this.observedFilters.account_id && ['price','stock'].includes(this.observedFilters.proposal_kind) && this.observedFilters.status === 'pending_review' && !this.error; },
      stockObservation() { return this.context?.stocks.find(row => row.warehouse_id === Number(this.warehouseId)) || null; },
      pricePreview() {
        if (this.kind !== 'price') return null;
        const raw = this.amount.trim().replace(/[\s\u00a0]/g,'').replace(',','.');
        if (!/^\d+(\.\d{1,2})?$/.test(raw)) return null;
        const [whole, fraction = ''] = raw.split('.');
        const rubles = Number(whole), cents = Number(fraction.padEnd(2, '0'));
        if (!Number.isSafeInteger(rubles) || rubles > 999999999) return null;
        const rounded = rubles + (cents >= 50 ? 1 : 0);
        return {price:String(rounded), changed:cents !== 0, valid:rounded >= 1 && rounded <= 999999999};
      },
    },
    mounted() {
      document.getElementById('oc-fallback')?.remove();
      this._visible = () => { clearTimeout(this.timer); clearTimeout(this.refreshTimer); if (!document.hidden) { this.schedule(); this.pollRefreshes(); } };
      this._pop = () => { if (this.mode === 'list' && !this.busy) { const q = new URLSearchParams(location.search); this.filters = {account_id:q.get('account_id') || '',proposal_kind:q.get('proposal_kind') || '',status:q.get('status') || ''}; this.loadList(Number(q.get('page')) || 1); } };
      this._leave = event => { if (this.busy || (this.createOpen && this.amount && !this.formBlocked)) { event.preventDefault(); event.returnValue = ''; } };
      document.addEventListener('visibilitychange', this._visible);
      window.addEventListener('popstate', this._pop);
      window.addEventListener('beforeunload', this._leave);
      if (this.mode === 'detail') this.loadDetail();
      else this.loadList(Number(new URLSearchParams(location.search).get('page')) || 1).then(() => {
        const id = initialListingId;
        if (id && /^[1-9]\d*$/.test(id) && !this.error) { this.openCreate(); this.chooseProduct(Number(id)); }
      });
    },
    beforeUnmount() {
      this.stopped = true; clearTimeout(this.timer); clearTimeout(this.refreshTimer); requests.forEach(controller => controller.abort());
      document.removeEventListener('visibilitychange', this._visible);
      window.removeEventListener('popstate', this._pop);
      window.removeEventListener('beforeunload', this._leave);
    },
    methods: {
      title(p) { return p.product?.title || p.proposed_state?.offer_id || 'Товар недоступен'; },
      photo(p) { const url = p?.primary_image; return typeof url === 'string' && /^https:\/\//.test(url) && !this.failedImages[url] ? url : ''; },
      imageError(p) { if (p?.primary_image) this.failedImages[p.primary_image] = true; },
      date(value) { if (!value) return 'Дата неизвестна'; const d = new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z'); return Number.isNaN(d.getTime()) ? 'Дата неизвестна' : new Intl.DateTimeFormat('ru-RU',{dateStyle:'short',timeStyle:'short'}).format(d); },
      money(value, currency) { if (!['string','number'].includes(typeof value) || !/^\d+(\.\d+)?$/.test(String(value)) || !Number.isFinite(Number(value))) return '—'; const formatted = new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(Number(value)); return formatted + (currency === 'RUB' ? ' ₽' : typeof currency === 'string' && /^[A-Z]{3}$/.test(currency) ? ' ' + currency : ' · валюта неизвестна'); },
      quantity(value) { return Number.isSafeInteger(value) && value >= 0 ? new Intl.NumberFormat('ru-RU').format(value) + ' шт.' : '—'; },
      value(p, side) { const state = p[side] || {}; return p.proposal_kind === 'price' ? this.money(state.price, state.currency_code) : this.quantity(state.stock); },
      reviewUrl(hold) {
        const id = hold?.operation_id;
        const url = hold?.review_url;
        return Number.isSafeInteger(id) && id > 0 && url === '/marketplaces/operations/' + id + '/review' ? url : '';
      },
      held(p) { return Boolean(p?.write_quarantine?.status === 'active'); },
      statusClass(status) { return status === 'applied' ? 'is-ok' : ['failed','conflict'].includes(status) ? 'is-error' : ['pending_review','uncertain'].includes(status) ? 'is-pending' : ''; },
      active(p) { return ['approved','applying','uncertain'].includes(p?.status); },
      statusDescription(p) {
        if (p.error_code === 'commercial_live_state_conflict') return 'После отправки Ozon вернул другое значение. Изменение могло примениться частично. Проверьте текущее состояние и подготовьте новое сравнение; повторной отправки не было.';
        const descriptions = {pending_review:'Заявка сохранена. Отправка изменения в Ozon ещё не выполнялась.',
          approved:'Изменение подтверждено. Ожидаем результат отправки и проверки.', applying:'Сверяем результат с Ozon. Можно вернуться позже: состояние заявки сохранено.',
          applied:'После отправки Ozon подтвердил точное предлагаемое значение.', rejected:'Заявка закрыта. Запрос изменения в Ozon не отправлялся.',
          conflict:'Данные в Ozon изменились после создания заявки. Подготовьте новое сравнение.',
          failed:'Изменение не подтверждено. Подробности доступны в журнале операции.', uncertain:'Точный результат пока неизвестен. Повторная отправка не выполняется; смотрите состояние сверки в журнале операции.', cancelled:'Заявка отменена.'};
        return descriptions[p.status] || 'Проверьте подробности операции.';
      },
      friendly(message) {
        const text = String(message || 'Не удалось выполнить действие. Обновите данные и повторите проверку.');
        if (/Новая цена должна оставаться ниже/.test(text)) return 'Новая цена должна быть ниже сохранённой цены до скидки.';
        if (/ниже текущей min_price/.test(text)) return 'Новая цена ниже минимальной цены, установленной в Ozon.';
        if (/Для price override/.test(text)) return 'Объясните разрешённое снижение или крупное изменение цены: не менее 8 символов.';
        if (/idempotency_key|активный commercial proposal/.test(text)) return 'Для этого товара уже есть заявка. Проверьте список изменений.';
        if (/Proposal уже изменился/.test(text)) return 'Заявка уже изменилась. Проверьте её состояние перед новым решением.';
        if (/Один из proposals уже изменился/.test(text)) return 'Одна из выбранных заявок уже изменилась. Общая отправка не начата. Проверьте состояния заявок.';
        if (/Live state части batch изменился/.test(text)) return 'Данные части выбранных товаров изменились в Ozon. Общая отправка остановлена; проверьте заявки.';
        if (/Live state не совпадает ни с before/.test(text)) return 'Ozon вернул значение, отличающееся от исходного и подтверждённого. Автоматический откат остановлен. Для нового решения сначала заново прочитайте данные Ozon.';
        if (/Live state изменился после создания proposal/.test(text)) return 'Данные изменились после подготовки сравнения. Запрос изменения не отправлялся.';
        if (/Only RUB/.test(text)) return 'Изменение цены доступно для товаров с валютой RUB.';
        return text.replace(/commercial workflow/g,'изменение').replace(/Commercial proposal/g,'Заявка').replace(/commercial proposal/g,'заявка').replace(/Proposal/g,'Заявка');
      },
      async request(url, body) {
        const controller = new AbortController(); requests.add(controller);
        const timer = setTimeout(() => controller.abort(), body === undefined ? 12000 : 45000);
        try {
          const response = await fetch(url, {method:body === undefined ? 'GET' : 'POST', credentials:'same-origin',
            headers:{Accept:'application/json', ...(body === undefined ? {} : {'Content-Type':'application/json','X-CSRFToken':document.querySelector('meta[name="csrf-token"]')?.content || ''})},
            body:body === undefined ? undefined : JSON.stringify(body), signal:controller.signal});
          if (response.redirected || [401,403].includes(response.status)) { this.sessionEnded = true; throw Object.assign(new Error('Сессия завершена или доступ изменился. Войдите снова.'),{status:response.status}); }
          const data = await response.json().catch(() => { throw new Error('Сервер вернул неполный ответ. Проверьте состояние перед повторным действием.'); });
          if (!response.ok || data.success !== true) throw Object.assign(new Error(this.friendly(data.error)),{
            status:response.status, code:data.code,
            writeQuarantine:data.code === 'ozon_write_quarantined' ? data.write_quarantine : null,
            nextAttemptAt:data.next_attempt_at || null,
          });
          return data;
        } catch (error) {
          if (error.name === 'AbortError' || error instanceof TypeError) throw new Error(body === undefined ? 'Не удалось получить данные. Проверьте соединение и обновите страницу.' : 'Ответ не получен. Действие могло выполниться. Проверьте состояние заявки перед повторной отправкой.');
          throw error;
        } finally { clearTimeout(timer); requests.delete(controller); }
      },
      async loadList(page) {
        if (this.loading || this.busy || this.sessionEnded || this.stopped) return;
        const revision = ++this.listRevision;
        const filters = clone(this.filters); const target = Math.max(1,page || this.pagination.page || 1);
        const query = new URLSearchParams({page:String(target),per_page:'50'});
        Object.entries(filters).forEach(([key,val]) => { if (val) query.set(key,val); });
        this.loading = true; this.error = ''; this.selected = [];
        try {
          const data = await this.request(this.base + '?' + query);
          if (revision !== this.listRevision || this.stopped) return;
          if (!Array.isArray(data.items) || !data.pagination || !Array.isArray(data.accounts)) throw new Error('Неполные данные списка. Обновите страницу.');
          this.items = data.items; this.accounts = data.accounts; this.warehouses = data.warehouses || [];
          this.pagination = data.pagination; this.writeEnabled = data.write_enabled; this.observedFilters = filters;
          const listingLink = new URLSearchParams(location.search).get('listing_id');
          if (listingLink && /^[1-9]\d*$/.test(listingLink)) query.set('listing_id',listingLink);
          history.replaceState({},'',this.base + '?' + query);
          if (filters.account_id) this.readRefresh('warehouses',Number(filters.account_id));
          else this.refreshError = '';
        } catch (error) { this.error = error.message; if (this.observedFilters.account_id !== undefined) this.filters = clone(this.observedFilters); }
        finally { this.loading = false; }
      },
      applyFilters() { this.notice = ''; this.loadList(1); },
      resetFilters() { this.filters = {...this.filters,proposal_kind:'',status:''}; this.loadList(1); },
      schedule() {
        clearTimeout(this.timer);
        if (this.mode !== 'detail' || this.stopped || this.sessionEnded || document.hidden || this.busy || !this.active(this.proposal) || this.polls >= 120) return;
        this.timer = setTimeout(() => { this.polls += 1; this.loadDetail(); }, this.pollDelay);
      },
      async loadDetail() {
        if (this.loading || this.busy || this.sessionEnded || this.stopped) return;
        this.loading = true; this.error = ''; this.errorQuarantine = null; this.confirmed = false;
        try {
          const data = await this.request(this.base + 'api/' + config.proposalId);
          if (!data.proposal || data.proposal.id !== config.proposalId) throw new Error('Получено другое изменение. Откройте заявку заново.');
          this.proposal = data.proposal; this.writeEnabled = data.write_enabled; this.actionBlocked = false; this.pollDelay = 5000;
        } catch (error) { this.error = error.message; this.actionBlocked = true; this.pollDelay = Math.min(this.pollDelay * 2,30000); }
        finally { this.loading = false; this.schedule(); }
      },
      async decision(action, body) {
        if (this.busy || this.loading || this.actionBlocked || this.sessionEnded) return;
        this.busy = action; this.error = ''; this.errorQuarantine = null; this.confirmed = false; clearTimeout(this.timer);
        try {
          const data = await this.request(this.base + this.proposal.id + '/' + action, body);
          if (!data.proposal || data.proposal.id !== this.proposal.id) throw new Error('Ответ не содержит состояния этой заявки. Проверьте состояние.');
          this.proposal = data.proposal;
        } catch (error) { this.error = error.message; this.errorQuarantine = error.writeQuarantine || null; this.actionBlocked = true; }
        finally { this.busy = false; this.schedule(); }
      },
      approve() { if (this.confirmed && this.proposal?.status === 'pending_review' && this.proposal.target_available && !this.held(this.proposal) && this.writeEnabled) this.decision('approve',{expected_version:this.proposal.version,confirm_write:true}); },
      reject() { if (this.proposal?.status === 'pending_review') this.decision('reject',{expected_version:this.proposal.version,note:this.rejectNote}); },
      async rollback() {
        if (this.busy || this.actionBlocked || this.proposal?.status !== 'applied') return;
        this.busy = 'rollback'; this.error = '';
        try {
          const data = await this.request(this.base + 'operations/' + this.proposal.operation_id + '/rollback-proposals', {idempotency_key:crypto.randomUUID()});
          if (!Number.isSafeInteger(data.proposal?.id)) throw new Error('Проверьте список заявок: ответ на подготовку отката неполный.');
          this.busy = false; location.assign(this.base + data.proposal.id);
        } catch (error) { this.error = error.message; this.actionBlocked = true; this.busy = false; }
      },
      openCreate() {
        if (this.busy) return;
        this._createTrigger = document.activeElement; this.context = null; this.products = []; this.query = ''; this.formError = ''; this.formBlocked = false; this.searched = false;
        this.stockRefresh = null; this.stockRefreshListing = null; this.formRefreshError = '';
        this.createAccount = this.observedFilters.account_id || (this.accounts.filter(a=>a.is_active).length === 1 ? String(this.accounts.find(a=>a.is_active).id) : '');
        this.kind = 'price'; this.resetInput(); this.createOpen = true; this.$refs.createDialog.showModal();
        if (this.createAccount) this.searchProducts(1);
      },
      closeCreate() {
        if (this.busy) return;
        if (this.amount && !this.formBlocked && !window.confirm('Закрыть форму? Введённое изменение ещё не сохранено.')) return;
        ++this.searchRevision; ++this.contextRevision; this.refreshRevisions = {}; this.createOpen = false; this.contextLoading = false; this.searching = false;
        const query = new URLSearchParams(location.search);
        if (query.has('listing_id')) { query.delete('listing_id'); history.replaceState({},'',this.base + '?' + query); }
        this.$refs.createDialog.close(); this._createTrigger?.focus();
      },
      changeProduct() { if (this.amount && !window.confirm('Выбрать другой товар? Введённое значение будет сброшено.')) return; this.context = null; this.stockRefresh = null; this.stockRefreshListing = null; this.formRefreshError = ''; this.resetInput(); this.formError = ''; this.searchProducts(1); },
      resetInput() { this.amount = ''; this.warehouseId = ''; this.allowDecrease = false; this.allowLarge = false; this.guardrailNote = ''; },
      async searchProducts(page) {
        const revision = ++this.searchRevision; this.products = []; this.searched = false; this.formError = ''; this.productPages = 0;
        if (!this.createAccount) { this.searching = false; return; }
        this.searching = true;
        try {
          const query = new URLSearchParams({marketplace:'ozon',account_id:this.createAccount,search:this.query.trim(),per_page:'20',page:String(page)});
          const data = await this.request(this.catalog + 'api?' + query);
          if (revision !== this.searchRevision || !this.createOpen) return;
          this.products = data.items.filter(p=>!p.is_archived && p.account_id === Number(this.createAccount)); this.productPage = page; this.productPages = data.pagination.pages;
          this.searched = true;
        } catch (error) { if (revision === this.searchRevision) this.formError = error.message; }
        finally { if (revision === this.searchRevision) this.searching = false; }
      },
      async chooseProduct(id) {
        if (this.busy) return;
        const revision = ++this.contextRevision; this.contextLoading = true; this.formError = '';
        try {
          const data = await this.request(this.base + 'listings/' + id + '/context');
          if (revision !== this.contextRevision || !this.createOpen) return;
          if (data.product?.id !== id || !Array.isArray(data.warehouses) || !Array.isArray(data.stocks)) throw new Error('Не удалось проверить данные товара. Выберите товар ещё раз.');
          this.context = data; this.createAccount = String(data.product.account_id); this.resetInput();
          this.readRefresh('fbs_stock',id);
          this.readRefresh('warehouses',data.product.account_id);
          this.$nextTick(() => { if (matchMedia('(min-width: 700px)').matches) this.$refs.amount?.focus(); });
        } catch (error) { if (revision === this.contextRevision) this.formError = error.message; }
        finally { if (revision === this.contextRevision) this.contextLoading = false; }
      },
      refreshUrl(kind,id) { return kind === 'warehouses' ? this.base + 'accounts/' + id + '/warehouses/refresh' : this.base + 'listings/' + id + '/stocks/refresh'; },
      refreshPostUrl(kind,id) { return kind === 'warehouses' ? this.base + 'accounts/' + id + '/warehouses/sync' : this.refreshUrl(kind,id); },
      refreshText(refresh) {
        if (!refresh) return 'Обновление ещё не запрошено.';
        const progress = refresh.pages_loaded ? ' Страниц прочитано: ' + refresh.pages_loaded + '.' : '';
        const due = refresh.status === 'waiting_provider' && refresh.next_attempt_at ? ' Продолжим после ' + this.date(refresh.next_attempt_at) + '.' : '';
        const last = refresh.last_completed_at ? ' Последний полный снимок: ' + this.date(refresh.last_completed_at) + '.' : ' Полного снимка пока нет.';
        return refresh.message + progress + due + last;
      },
      async readRefresh(kind,id) {
        if (!Number.isSafeInteger(Number(id)) || Number(id) <= 0 || this.stopped) return;
        const key = kind + ':' + id;
        const revision = (this.refreshRevisions[key] || 0) + 1;
        this.refreshRevisions[key] = revision;
        try {
          const data = await this.request(this.refreshUrl(kind,id));
          if (this.stopped || revision !== this.refreshRevisions[key]) return;
          if (data.refresh && (data.refresh.kind !== kind || (kind === 'warehouses' ? data.refresh.account_id !== Number(id) : data.refresh.listing_id !== Number(id)))) throw new Error('Получено состояние другого обновления. Откройте товар заново.');
          if (typeof data.csrf_token !== 'string' || !data.csrf_token) throw new Error('Не удалось восстановить защищённую сессию. Обновите страницу.');
          const csrf = document.querySelector('meta[name="csrf-token"]');
          if (csrf) csrf.content = data.csrf_token;
          this.sessionEnded = false;
          if (kind === 'warehouses') {
            if (this.observedFilters.account_id !== String(id) && this.context?.product?.account_id !== Number(id)) return;
            this.warehouseRefreshes[id] = data.refresh; this.refreshError = '';
          } else {
            if (!this.createOpen || this.context?.product?.id !== Number(id)) return;
            this.stockRefresh = data.refresh; this.stockRefreshListing = Number(id); this.formRefreshError = '';
          }
          if (data.refresh?.status === 'completed' && !this.refreshLoaded.has(data.refresh.id)) {
            this.refreshLoaded.add(data.refresh.id);
            if (kind === 'warehouses') {
              if (this.observedFilters.account_id === String(id)) this.loadList();
              if (this.createOpen && this.context?.product?.account_id === Number(id)) this.reloadContext(this.context.product.id);
            } else if (this.createOpen) this.reloadContext(id);
          }
        } catch (error) {
          if (revision !== this.refreshRevisions[key]) return;
          if (kind === 'warehouses') this.refreshError = error.message;
          else this.formRefreshError = error.message;
        } finally { this.scheduleRefreshPoll(); }
      },
      scheduleRefreshPoll() {
        clearTimeout(this.refreshTimer);
        if (this.stopped || this.sessionEnded || document.hidden) return;
        const listAccount = this.observedFilters.account_id;
        const formAccount = this.createOpen ? this.context?.product?.account_id : null;
        if (this.warehouseRefreshes[listAccount]?.active || this.warehouseRefreshes[formAccount]?.active || (this.createOpen && this.stockRefresh?.active)) this.refreshTimer = setTimeout(() => this.pollRefreshes(),15000);
      },
      async pollRefreshes() {
        if (this.stopped || this.sessionEnded || document.hidden) return;
        if (this.observedFilters.account_id) await this.readRefresh('warehouses',Number(this.observedFilters.account_id));
        if (this.createOpen && this.context?.product?.account_id && String(this.context.product.account_id) !== this.observedFilters.account_id) await this.readRefresh('warehouses',this.context.product.account_id);
        if (this.createOpen && this.context?.product?.id) await this.readRefresh('fbs_stock',this.context.product.id);
        this.scheduleRefreshPoll();
      },
      async reloadContext(id) {
        try {
          const data = await this.request(this.base + 'listings/' + id + '/context');
          if (this.createOpen && this.context?.product?.id === id && data.product?.id === id) {
            this.context = data;
            if (!data.warehouses.some(w=>w.id === Number(this.warehouseId))) this.warehouseId = '';
          }
        } catch (error) { this.formRefreshError = error.message; }
      },
      async startRefresh(kind,id) {
        if (this.busy || this.loading || this.sessionEnded || this.stopped) return;
        this.busy = kind; this.refreshError = ''; this.formRefreshError = '';
        try {
          const data = await this.request(this.refreshPostUrl(kind,id),{});
          if (!data.refresh || data.refresh.kind !== kind || (kind === 'warehouses' ? data.refresh.account_id !== Number(id) : data.refresh.listing_id !== Number(id))) throw new Error('Сервер не подтвердил область обновления. Прочитайте состояние перед повтором.');
          if (kind === 'warehouses') this.warehouseRefreshes[id] = data.refresh;
          else { this.stockRefresh = data.refresh; this.stockRefreshListing = Number(id); }
        } catch (error) {
          // A lost POST response may have enqueued the job. A GET is safe; never send a second POST automatically.
          if (/Ответ не получен|неполный ответ/.test(error.message)) {
            await this.readRefresh(kind,id);
            const recovered = kind === 'warehouses' ? this.warehouseRefreshes[id] : this.stockRefresh;
            if (!recovered?.active) {
              const message = 'Ответ на постановку не получен. Активное обновление не найдено; проверьте данные перед новым нажатием.';
              if (kind === 'warehouses') this.refreshError = message; else this.formRefreshError = message;
            }
          }
          else if (kind === 'warehouses') this.refreshError = error.message + (error.nextAttemptAt ? ' После ' + this.date(error.nextAttemptAt) + '.' : '');
          else this.formRefreshError = error.message + (error.nextAttemptAt ? ' После ' + this.date(error.nextAttemptAt) + '.' : '');
        } finally { this.busy = false; this.scheduleRefreshPoll(); }
      },
      syncWarehouses(id) { return this.startRefresh('warehouses',Number(id)); },
      refreshStocks(id) { return this.startRefresh('fbs_stock',Number(id)); },
      async createProposal() {
        if (this.busy || this.formBlocked || !this.context || this.sessionEnded) return;
        this.formError = ''; const raw = this.amount.trim().replace(/[\s\u00a0]/g,'').replace(',','.');
        let body;
        if (this.kind === 'price') {
          if (!/^\d+(\.\d{1,2})?$/.test(raw) || Number(raw) <= 0) this.formError = 'Введите положительную цену с точностью до копейки.';
          else if (!this.pricePreview?.valid) this.formError = 'Цена после округления должна быть от 1 до 999 999 999 ₽.';
          else if ((this.allowDecrease || this.allowLarge) && this.guardrailNote.trim().length < 8) this.formError = 'Объясните изменение: не менее 8 символов.';
          body = {price:raw,allow_price_decrease:this.allowDecrease,allow_large_change:this.allowLarge,guardrail_note:this.guardrailNote.trim() || null};
        } else {
          if (!/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw)) || Number(raw) > 2147483647) this.formError = 'Введите целое количество от 0 до 2 147 483 647.';
          else if (!this.context.warehouses.some(w=>w.id === Number(this.warehouseId))) this.formError = 'Выберите доступный склад этого магазина.';
          body = {stock:Number(raw),warehouse_id:Number(this.warehouseId)};
        }
        if (this.formError) { this.$nextTick(()=>this.$refs.formError?.focus()); return; }
        body.idempotency_key = crypto.randomUUID(); this.busy = 'create';
        try {
          const data = await this.request(this.base + 'listings/' + this.context.product.id + '/' + this.kind + '-proposals',body);
          if (!Number.isSafeInteger(data.proposal?.id) || data.proposal.listing_id !== this.context.product.id) throw new Error('Проверьте список заявок: ответ на создание неполный.');
          this.formBlocked = true; this.busy = false; location.assign(this.base + data.proposal.id);
        } catch (error) {
          this.formError = error.message; this.formBlocked = ![400,404,422,429].includes(error.status);
          this.$nextTick(()=>this.$refs.formError?.focus());
        } finally { this.busy = false; }
      },
      openBatch() {
        if (!this.batchMode || this.busy || this.loading) return;
        const review = this.items.filter(p=>this.selected.includes(p.id));
        if (!review.length || review.length !== this.selected.length || review.length > 100 || review.some(p=>p.status !== 'pending_review' || !p.target_available)) return;
        this.batchReview = clone(review); this.batchConfirmed = false; this.batchBlocked = false; this.batchError = ''; this.batchErrorQuarantine = null;
        this._batchTrigger = document.activeElement; this.$refs.batchDialog.showModal();
      },
      async closeBatch() {
        if (this.busy) return;
        this.$refs.batchDialog.close(); this._batchTrigger?.focus();
        if (this.batchBlocked) await this.loadList();
      },
      async approveBatch() {
        if (this.busy || this.batchBlocked || !this.batchConfirmed || !this.batchReview.length || !this.writeEnabled || this.batchReview.some(p=>this.held(p))) return;
        this.busy = 'batch'; this.batchConfirmed = false; this.batchError = ''; this.batchErrorQuarantine = null;
        const expected = this.batchReview.map(p=>({proposal_id:p.id,expected_version:p.version}));
        try {
          const data = await this.request(this.base + 'batch-approve',{items:expected,confirm_write:true});
          if (!Array.isArray(data.items) || data.items.length !== expected.length || new Set(data.items.map(p=>p.id)).size !== expected.length || data.items.some(p=>!expected.some(x=>x.proposal_id === p.id))) throw new Error('Получен неполный результат. Проверьте состояние всех выбранных заявок.');
          const counts = {};
          data.items.forEach(p=>{ const label = statuses[p.status] || p.status; counts[label] = (counts[label] || 0) + 1; });
          this.notice = Object.entries(counts).map(([key,count])=>key + ': ' + count).join('. ') + '.';
          this.selected = []; this.$refs.batchDialog.close(); this.busy = false; await this.loadList(); this._batchTrigger?.focus();
        } catch (error) { this.batchError = error.message; this.batchErrorQuarantine = error.writeQuarantine || null; this.batchBlocked = true; }
        finally { this.busy = false; }
      },
    },
  });
  app.directive('image-deadline',window.mcatShared.imageDeadline);
  app.mount(host);
})();

// The classic pages keep the same durable read flow when Vue is unavailable.
(function () {
  'use strict';
  for (const form of document.querySelectorAll('form[data-oc-refresh-form]')) {
    const box = form.parentElement?.parentElement?.querySelector('[data-oc-refresh-status]') || form.closest('section')?.querySelector('[data-oc-refresh-status]');
    const button = form.querySelector('button[type="submit"]');
    const statusUrl = form.dataset.statusUrl;
    if (!box || !statusUrl || !button) continue;
    let timer = null, stopped = false, sawActive = false, busy = false;
    const formatDate = value => {
      const date = new Date(value || '');
      return Number.isNaN(date.getTime()) ? 'неизвестно' : new Intl.DateTimeFormat('ru-RU',{dateStyle:'short',timeStyle:'short'}).format(date);
    };
    const show = refresh => {
      if (!refresh) { box.textContent = 'Обновление ещё не запрошено. Последние полные данные остаются доступными.'; button.disabled = false; return; }
      const text = [refresh.message || 'Проверьте состояние обновления.'];
      if (refresh.pages_loaded) text.push('Страниц прочитано: ' + refresh.pages_loaded + '.');
      if (refresh.status === 'waiting_provider' && refresh.next_attempt_at) text.push('Продолжим после ' + formatDate(refresh.next_attempt_at) + '.');
      text.push(refresh.last_completed_at ? 'Последний полный снимок: ' + formatDate(refresh.last_completed_at) + '.' : 'Полного снимка пока нет.');
      box.textContent = text.join(' ');
      button.disabled = !!refresh.active;
      if (refresh.active) sawActive = true;
      if (refresh.status === 'completed' && sawActive) {
        sawActive = false;
        location.reload();
      }
    };
    const schedule = refresh => {
      clearTimeout(timer);
      if (!stopped && !document.hidden && refresh?.active) timer = setTimeout(read, refresh.status === 'waiting_provider' ? 30000 : 15000);
    };
    const read = async () => {
      if (stopped || document.hidden || busy) return;
      try {
        const response = await fetch(statusUrl,{credentials:'same-origin',headers:{Accept:'application/json'}});
        if (response.redirected || [401,403].includes(response.status)) throw new Error('Сессия завершена. Войдите снова и прочитайте состояние обновления.');
        const data = await response.json();
        if (!response.ok || data.success !== true) throw new Error(data.error || 'Не удалось прочитать состояние обновления.');
        if (typeof data.csrf_token !== 'string' || !data.csrf_token) throw new Error('Не удалось восстановить защищённую сессию. Обновите страницу.');
        const csrf = form.querySelector('[name="csrf_token"]');
        if (csrf) csrf.value = data.csrf_token;
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) meta.content = data.csrf_token;
        show(data.refresh); schedule(data.refresh);
      } catch (error) { box.textContent = error.message || 'Не удалось прочитать состояние обновления.'; button.disabled = false; clearTimeout(timer); }
    };
    form.addEventListener('submit',async event => {
      event.preventDefault();
      if (busy || button.disabled) return;
      busy = true; button.disabled = true; box.textContent = 'Ставим обновление в очередь. Текущие значения не изменятся до полного снимка.';
      try {
        const response = await fetch(form.action,{method:'POST',credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/json','X-CSRFToken':form.querySelector('[name="csrf_token"]')?.value || ''},body:'{}'});
        if (response.redirected || [401,403].includes(response.status)) throw Object.assign(new Error('Сессия завершена или доступ изменился. Войдите снова.'),{server:true});
        const data = await response.json();
        if (!response.ok || data.success !== true) throw Object.assign(new Error((data.error || 'Обновление не поставлено в очередь.') + (data.next_attempt_at ? ' После ' + formatDate(data.next_attempt_at) + '.' : '')),{server:true});
        show(data.refresh); schedule(data.refresh);
      } catch (error) {
        if (error.server) { box.textContent = error.message; button.disabled = false; return; }
        // The POST can be lost after enqueue. Read the deduplicated scope, never retry it automatically.
        await read();
        if (!button.disabled) box.textContent = 'Ответ на постановку не получен. ' + (box.textContent || 'Проверьте состояние перед новым нажатием.');
      } finally { busy = false; }
    });
    document.addEventListener('visibilitychange',() => { clearTimeout(timer); if (!document.hidden) read(); });
    window.addEventListener('pagehide',() => { stopped = true; clearTimeout(timer); });
    read();
  }
})();
