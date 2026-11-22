/* Основной каталог маркетплейсов — Vue 3 (CDN, без бандлера).
   Read-only витрина поверх существующего JSON API + существующий Ozon-синк. */
(function () {
    'use strict';

    var bootstrapEl = document.getElementById('mcat-bootstrap');
    var appEl = document.getElementById('marketplace-catalog-app');
    if (!bootstrapEl || !appEl || typeof Vue === 'undefined') return;

    var bootstrap;
    try {
        bootstrap = JSON.parse(bootstrapEl.textContent);
    } catch (err) {
        return;
    }

    var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

    var SYNC_STATUS_RU = {
        running: 'выполняется',
        paused: 'на паузе',
        completed: 'завершён',
        failed: 'ошибка'
    };

    var S = window.mcatShared;
    var syncTimer, cooldownWakeTimer, syncController, syncRevision = 0, syncStopped = false;
    var syncRequests = new Set();
    var LISTING_UPDATE_MAX = 200;
    var LISTING_UPDATE_PREVIEW_TIMEOUT_MS = 15000;
    var LISTING_UPDATE_APPLY_TIMEOUT_MS = 45000;
    var LISTING_UPDATE_PREVIEW_TIMEOUT_MESSAGE = 'Проверка точного списка заняла слишком много времени. Выбор сохранён; перечитайте список перед подтверждением.';
    var LISTING_UPDATE_APPLY_TIMEOUT_MESSAGE = 'Ответ на локальное сохранение не пришёл за 45 секунд. Результат неизвестен; проверьте выбранный список заново.';
    var LISTING_UPDATE_STORAGE_KEY = 'mcat-ozon-update-selection-v1';
    function activeJob(job) { return !!job && ['pending', 'running'].includes(job.status); }
    function saved(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
    function remember(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* Optional preference. */ } }
    function positiveIdList(value, max) {
        return Array.isArray(value) && value.length > 0 && value.length <= max &&
            value.every(function (id) { return Number.isSafeInteger(id) && id > 0; }) &&
            new Set(value).size === value.length;
    }
    function sameOrderedIds(left, right) {
        return Array.isArray(left) && Array.isArray(right) && left.length === right.length &&
            left.every(function (id, index) { return id === right[index]; });
    }
    function validListingUpdateRow(row, listingId, statuses) {
        return !!row && Number.isSafeInteger(row.listing_id) && row.listing_id === listingId &&
            typeof row.title === 'string' && row.title.length <= 500 &&
            typeof row.offer_id === 'string' && row.offer_id.length <= 300 &&
            statuses.includes(row.status) &&
            (row.message === undefined ||
                (typeof row.message === 'string' && row.message.length <= 1000));
    }
    function validListingUpdatePreview(value, snapshot) {
        if (!value || !Number.isSafeInteger(value.account_id) ||
            value.account_id !== snapshot.accountId ||
            value.account_label !== snapshot.accountLabel ||
            !positiveIdList(value.listing_ids, LISTING_UPDATE_MAX) ||
            !sameOrderedIds(value.listing_ids, snapshot.listingIds) ||
            typeof value.token !== 'string' || !value.token || value.token.length > 8192 ||
            !Array.isArray(value.rows) || value.rows.length !== snapshot.listingIds.length) return false;
        return value.rows.every(function (row, index) {
            return validListingUpdateRow(row, snapshot.listingIds[index], ['ready', 'existing', 'blocked']);
        });
    }
    function validListingUpdateReport(value, snapshot) {
        if (!value || !Number.isSafeInteger(value.account_id) ||
            value.account_id !== snapshot.accountId ||
            value.account_label !== snapshot.accountLabel ||
            !positiveIdList(value.listing_ids, LISTING_UPDATE_MAX) ||
            !sameOrderedIds(value.listing_ids, snapshot.listingIds) ||
            !Array.isArray(value.rows) || value.rows.length !== snapshot.listingIds.length) return false;
        return value.rows.every(function (row, index) {
            if (!validListingUpdateRow(row, snapshot.listingIds[index], ['created', 'existing', 'blocked'])) {
                return false;
            }
            if (row.status === 'blocked') return !Object.prototype.hasOwnProperty.call(row, 'draft_id');
            return Number.isSafeInteger(row.draft_id) && row.draft_id > 0;
        });
    }
    function utcDate(value) {
        if (typeof value !== 'string' || !value) return null;
        var parsed = new Date(/[Zz]|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z');
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    Vue.createApp({
        directives: {'image-deadline': S.imageDeadline},
        components: {'ozon-prices': S.ozonPrices},
        data: function () {
            return {
                ozonEnabled: !!bootstrap.ozonEnabled,
                listingUpdateOwner: typeof bootstrap.selectionOwner === 'string'
                    ? bootstrap.selectionOwner : '',
                accounts: (bootstrap.accounts || []).map(function (acc) {
                    acc._syncing = false;
                    acc._error = null;
                    if (!Object.prototype.hasOwnProperty.call(acc, 'last_completed_at')) acc.last_completed_at = null;
                    return acc;
                }),
                urls: bootstrap.urls || {},
                illustration: bootstrap.illustration || '',
                perPage: bootstrap.perPage || 60,
                catalogReturnUrl: bootstrap.catalogReturnUrl || '/marketplaces/listings/',
                requestedPage: bootstrap.catalogPage || 1,
                loadedStartPage: bootstrap.catalogPage || 1,
                historyRestoring: false,

                items: [],
                pagination: { page: 1, pages: 1, total: 0, has_next: false },
                loading: true,
                loadingMore: false,
                syncError: '', syncFailures: 0, syncSessionExpired: false,
                cooldownClock: Date.now(),
                error: null,
                animate: false,
                advancedFiltersOpen: false,

                filters: {
                    marketplace: (bootstrap.filters || {}).marketplace_code || null,
                    account_id: (bootstrap.filters || {}).account_id || null,
                    status: (bootstrap.filters || {}).normalized_status || '',
                    link_status: (bootstrap.filters || {}).link_status || '',
                    include_unavailable: !!(bootstrap.filters || {}).include_unavailable,
                    search: (bootstrap.filters || {}).search || ''
                },
                searchInput: (bootstrap.filters || {}).search || '',
                counts: { all: null, active: null, moderation: null, error: null, archived: null },
                channelCounts: {},
                displayTotal: 0,

                view: saved('sh-mcat-view') === 'table' ? 'table' : 'grid',
                mode: saved('sh-mcat-mode') === 'listings' ? 'listings' : 'products',
                selectedIndex: -1,
                listingUpdate: {
                    selectedIds: [], phase: 'idle', preview: null, report: null,
                    editorUrl: null, error: '', accountId: null, returnTo: '', confirmLocal: false,
                    selectionScopeKey: '',
                },
                imgFailed: {},
                hoverFailed: {},
                failedImages: {},
                hovered: {},

                drawer: { open: false, loading: false, error: null, detail: null, image: null, memberIdx: 0, link: null },

                statusChips: [
                    { key: 'all', value: '', label: 'Все' },
                    { key: 'active', value: 'active', label: 'Активные' },
                    { key: 'moderation', value: 'moderation', label: 'Модерация' },
                    { key: 'error', value: 'error', label: 'Ошибки' },
                    { key: 'archived', value: 'archived', label: 'Архив' }
                ]
            };
        },

        computed: {
            myProductsUrl: function () {
                var scope = new URLSearchParams();
                if (this.filters.marketplace) scope.set('marketplace', this.filters.marketplace);
                if (this.filters.account_id) scope.set('account_id', this.filters.account_id);
                return this.urls.myProducts + (scope.size ? '?' + scope.toString() : '');
            },
            draftsUrl: function () {
                return this.urls.drafts + (this.filters.marketplace === 'ozon' && this.filters.account_id
                    ? '?account_id=' + encodeURIComponent(this.filters.account_id) : '');
            },
            visibleAccounts: function () {
                if (!this.ozonEnabled || this.filters.marketplace === 'wb') return [];
                return this.accounts.filter(acc => !this.filters.account_id || acc.id === this.filters.account_id);
            },
            classicUrl: function () {
                return this.catalogUrl(this.pageForGroup(this.selectedIndex), this.urls.classic);
            },
            channelKey: function () {
                if (this.filters.marketplace === 'wb') return 'wb';
                if (this.filters.marketplace === 'ozon') {
                    return this.filters.account_id ? 'ozon:' + this.filters.account_id : 'ozon';
                }
                return 'all';
            },
            listingUpdateScopeEligible: function () {
                var accountId = Number(this.filters.account_id);
                return this.ozonEnabled && this.mode === 'listings' &&
                    this.filters.marketplace === 'ozon' &&
                    Number.isSafeInteger(accountId) && accountId > 0 &&
                    this.accounts.some(function (account) { return account.id === accountId; });
            },
            listingUpdateCanSwitchToListings: function () {
                var accountId = Number(this.filters.account_id);
                return this.ozonEnabled && this.mode === 'products' &&
                    this.filters.marketplace === 'ozon' &&
                    Number.isSafeInteger(accountId) && accountId > 0 &&
                    this.accounts.some(function (account) { return account.id === accountId; });
            },
            visibleListingUpdateIds: function () {
                if (!this.listingUpdateScopeEligible) return [];
                var accountId = Number(this.filters.account_id);
                var seen = new Set();
                var ids = [];
                for (var i = 0; i < this.items.length; i += 1) {
                    var group = this.items[i];
                    var row = group && group._p;
                    if (!group || !Array.isArray(group.listings) || group.listings.length !== 1 ||
                        (group.listing_count !== undefined && group.listing_count !== 1) ||
                        !row || row.marketplace_code !== 'ozon' || row.account_id !== accountId ||
                        !Number.isSafeInteger(row.id) || row.id <= 0 || seen.has(row.id)) continue;
                    seen.add(row.id);
                    ids.push(row.id);
                }
                return ids;
            },
            listingUpdateAllVisibleSelected: function () {
                var ids = this.visibleListingUpdateIds;
                return ids.length > 0 && ids.every((id) => this.listingUpdate.selectedIds.includes(id));
            },
            listingUpdateSomeVisibleSelected: function () {
                var ids = this.visibleListingUpdateIds;
                return ids.some((id) => this.listingUpdate.selectedIds.includes(id));
            },
            listingUpdateCanPreview: function () {
                return this.listingUpdateScopeEligible && !this.loading && !this.loadingMore &&
                    this.listingUpdate.selectedIds.length > 0 &&
                    this.listingUpdate.selectedIds.length <= LISTING_UPDATE_MAX &&
                    this.listingUpdate.selectionScopeKey === this.listingUpdateScopeKey() &&
                    positiveIdList(this.listingUpdate.selectedIds, LISTING_UPDATE_MAX);
            },
            listingUpdatePreviewLabel: function () {
                if (this.listingUpdate.phase === 'complete') return 'Открыть итог подготовки';
                if (this.listingUpdate.phase === 'read_failed' ||
                    this.listingUpdate.phase === 'apply_uncertain') return 'Проверить ещё раз';
                return 'Подготовить обновления';
            },
            listingUpdateCanApply: function () {
                return this.listingUpdate.phase === 'review' && !!this.listingUpdate.preview &&
                    this.listingUpdate.preview.rows.some(function (row) { return row.status !== 'blocked'; });
            },
            activeGroup: function () {
                return this.items[this.selectedIndex] || null;
            },
            current: function () {
                var group = this.activeGroup;
                if (!group) return null;
                var idx = Math.min(this.drawer.memberIdx, group.listings.length - 1);
                return group.listings[Math.max(0, idx)] || null;
            },
            detail: function () {
                var d = this.drawer.detail;
                return d && this.current && d.id === this.current.id ? d : null;
            },
            moderationErrors: function () {
                var raw = (this.detail && this.detail.moderation_errors) || [];
                return raw.slice(0, 20).map(function (entry) {
                    if (typeof entry === 'string') return entry;
                    if (entry && typeof entry === 'object') {
                        return entry.description || entry.message || entry.error ||
                            entry.code || JSON.stringify(entry).slice(0, 200);
                    }
                    return String(entry);
                });
            },
            targetTotal: function () {
                // pagination.total уже учитывает все активные фильтры в обоих
                // режимах; counts.all — только для бейджа чипа «Все».
                return this.pagination.total || 0;
            },
            displayTotalFormatted: function () {
                return new Intl.NumberFormat('ru-RU').format(this.displayTotal);
            },
            totalNoun: function () {
                return this.mode === 'products'
                    ? this.plural(this.targetTotal, 'товар', 'товара', 'товаров')
                    : this.plural(this.targetTotal, 'листинг', 'листинга', 'листингов');
            },
            drawerImageSrc: function () {
                if (this.drawer.image && !this.failedImages[this.drawer.image]) return this.drawer.image;
                if (this.current && this.current.primary_image && !this.imgFailed[this.current.id]) {
                    return this.failedImages[this.current.primary_image] ? null : this.current.primary_image;
                }
                return null;
            },
            canonicalCard: function () {
                var link = this.drawer.link;
                var canonical = link && link.canonical_product;
                return canonical && this.current
                    && canonical.id === this.current.imported_product_id
                    ? canonical : null;
            },
            galleryImages: function () {
                var urls = [];
                var push = function (value) {
                    if (typeof value === 'string' && /^https?:\/\//.test(value) && urls.indexOf(value) === -1) {
                        urls.push(value);
                    }
                };
                var media = (this.detail && this.detail.media) || {};
                push(media.primary_image);
                (Array.isArray(media.images) ? media.images : []).forEach(push);
                if (this.current) {
                    push(this.current.primary_image);
                    push(this.current.hover_image);
                }
                return urls.filter(url => !this.failedImages[url]).slice(0, 8);
            },
            syncableAccount: function () {
                var self = this;
                return this.visibleAccounts.filter(function (acc) {
                    return self.canSync(acc);
                })[0] || null;
            },
            hasSearchFilters: function () {
                return !!(this.filters.status || this.filters.link_status ||
                    this.filters.include_unavailable || this.filters.search);
            },
            emptyState: function () {
                if (this.hasSearchFilters) {
                    return {
                        title: 'По этим фильтрам ничего не нашлось',
                        text: 'Попробуйте убрать часть условий или изменить запрос.',
                        action: 'reset'
                    };
                }
                if (this.filters.marketplace === 'wb' || !this.ozonEnabled) {
                    return {title: 'Каталог Wildberries пока пуст', text: 'После синхронизации товаров Wildberries их карточки появятся здесь.', action: 'none'};
                }
                if (this.ozonEnabled && !this.visibleAccounts.length) {
                    return {
                        title: 'Каталог пока пуст',
                        text: 'Подключите кабинет Ozon, чтобы его карточки появились здесь. Карточки Wildberries подтянутся после синхронизации товаров.',
                        action: 'accounts'
                    };
                }
                if (this.visibleAccounts.some(acc => this.syncActive(acc))) {
                    return {title: 'Загружаем ваш каталог', text: 'Товары появятся после загрузки. Страницу можно закрыть — прогресс сохранится.', action: 'sync'};
                }
                if (!this.syncableAccount) {
                    return {title: 'Подключите магазин', text: 'Проверьте подключение и срок ключа в настройках кабинета. Затем можно будет загрузить товары.', action: 'accounts'};
                }
                if (this.ozonEnabled) {
                    return {
                        title: 'Каталог пока пуст',
                        text: 'Загрузите существующие товары из Ozon или подготовьте первую карточку для нового магазина.',
                        action: 'sync'
                    };
                }
                return {
                    title: 'Каталог пока пуст',
                    text: 'Здесь появятся карточки Wildberries после синхронизации товаров.',
                    action: 'none'
                };
            }
        },

        watch: {
            searchInput: function (value) {
                var self = this;
                clearTimeout(this._searchTimer);
                this.clearListingUpdateSelection();
                this._searchTimer = setTimeout(function () {
                    var next = (value || '').trim().slice(0, 200);
                    if (next !== self.filters.search) self.filters.search = next;
                }, 300);
            },
            filters: {
                deep: true,
                handler: function () {
                    var currentScope = this.listingUpdateScopeKey();
                    var retainedScope = this.listingUpdate.selectionScopeKey;
                    if (currentScope && retainedScope === currentScope &&
                        (!this._listingUpdateSnapshot || this._listingUpdateSnapshot.scopeKey === currentScope)) return;
                    this.clearListingUpdateSelection();
                    if (this.historyRestoring) return;
                    this.requestedPage = 1;
                    this.loadedStartPage = 1;
                    history.replaceState(history.state, '', this.catalogUrl(1));
                    this.refresh();
                }
            },
            perPage: function () {
                this.clearListingUpdateSelection();
                if (this.historyRestoring) return;
                this.requestedPage = 1;
                this.loadedStartPage = 1;
                history.replaceState(history.state, '', this.catalogUrl(1));
                this.refresh();
            },
            'drawer.open': function (open) {
                document.body.style.overflow = open ? 'hidden' : '';
            },
            targetTotal: function (value) {
                this.animateTotal(value);
            }
        },

        mounted: function () {
            this._onKey = this.onKey.bind(this);
            this._onPopState = this.onPopState.bind(this);
            window.addEventListener('keydown', this._onKey);
            window.addEventListener('popstate', this._onPopState);
            this._onVisibility = () => {
                clearTimeout(syncTimer);
                if (document.hidden) {
                    clearTimeout(cooldownWakeTimer);
                    if (syncController) syncController.abort();
                } else {
                    this.cooldownClock = Date.now();
                    this.scheduleCooldownWake();
                    this.scheduleSyncPoll();
                }
            };
            document.addEventListener('visibilitychange', this._onVisibility);
            this.scheduleCooldownWake();
            this.scheduleSyncPoll();
            this.restoreListingUpdateSelection();
            this.refresh();
        },
        beforeUnmount: function () {
            syncStopped = true;
            clearTimeout(syncTimer);
            clearTimeout(cooldownWakeTimer);
            clearTimeout(this._searchTimer);
            if (this._totalRaf) cancelAnimationFrame(this._totalRaf);
            if (syncController) syncController.abort();
            if (this._listAbort) this._listAbort.abort();
            if (this._facetAbort) this._facetAbort.abort();
            if (this._detailAbort) this._detailAbort.abort();
            if (this._listingUpdateAbort) this._listingUpdateAbort.abort();
            this.clearListingUpdateTimeout();
            syncRequests.forEach(controller => controller.abort());
            document.removeEventListener('visibilitychange', this._onVisibility);
            window.removeEventListener('keydown', this._onKey);
            window.removeEventListener('popstate', this._onPopState);
            document.body.style.overflow = '';
        },

        methods: {
            clearListingUpdateTimeout: function (expectedTimeout) {
                var timeout = this._listingUpdateTimeout;
                if (timeout === null || timeout === undefined ||
                    (arguments.length && timeout !== expectedTimeout)) return false;
                clearTimeout(timeout);
                this._listingUpdateTimeout = null;
                return true;
            },
            listingUpdateScopeKey: function () {
                if (!this.listingUpdateScopeEligible) return '';
                return JSON.stringify({
                    owner: this.listingUpdateOwner,
                    marketplace: 'ozon',
                    account_id: Number(this.filters.account_id),
                    mode: 'listings',
                    status: this.filters.status || '',
                    link_status: this.filters.link_status || '',
                    include_unavailable: !!this.filters.include_unavailable,
                    search: (this.filters.search || '').trim().slice(0, 200),
                });
            },
            persistListingUpdateSelection: function () {
                try {
                    if (!this.listingUpdate.selectedIds.length) {
                        this.listingUpdate.selectionScopeKey = '';
                        sessionStorage.removeItem(LISTING_UPDATE_STORAGE_KEY);
                        return;
                    }
                    this.listingUpdate.selectionScopeKey = this.listingUpdateScopeKey();
                    sessionStorage.setItem(LISTING_UPDATE_STORAGE_KEY, JSON.stringify({
                        owner: this.listingUpdateOwner,
                        scope: this.listingUpdate.selectionScopeKey,
                        listing_ids: this.listingUpdate.selectedIds.slice(),
                    }));
                } catch (_) { /* The current page still keeps an in-memory exact set. */ }
            },
            restoreListingUpdateSelection: function () {
                if (!this.listingUpdateScopeEligible || !this.listingUpdateOwner) return false;
                try {
                    var raw = sessionStorage.getItem(LISTING_UPDATE_STORAGE_KEY);
                    if (!raw) return false;
                    var stored = JSON.parse(raw);
                    if (!stored || stored.owner !== this.listingUpdateOwner ||
                        stored.scope !== this.listingUpdateScopeKey() ||
                        !positiveIdList(stored.listing_ids, LISTING_UPDATE_MAX)) {
                        sessionStorage.removeItem(LISTING_UPDATE_STORAGE_KEY);
                        return false;
                    }
                    this.listingUpdate.selectedIds = stored.listing_ids.slice();
                    this.listingUpdate.selectionScopeKey = this.listingUpdateScopeKey();
                    return true;
                } catch (_) {
                    try { sessionStorage.removeItem(LISTING_UPDATE_STORAGE_KEY); } catch (__) {}
                    return false;
                }
            },
            clearListingUpdateSelection: function () {
                if (this._listingUpdateAbort && this.listingUpdate.phase !== 'applying') {
                    this._listingUpdateAbort.abort();
                    this._listingUpdateAbort = null;
                    this._listingUpdateRevision = (this._listingUpdateRevision || 0) + 1;
                    this.clearListingUpdateTimeout();
                }
                try { sessionStorage.removeItem(LISTING_UPDATE_STORAGE_KEY); } catch (_) {}
                this.listingUpdate.selectedIds = [];
                this.listingUpdate.selectionScopeKey = '';
                if (this.listingUpdate.phase === 'applying') return;
                this.listingUpdate.phase = 'idle';
                this.listingUpdate.preview = null;
                this.listingUpdate.report = null;
                this.listingUpdate.editorUrl = null;
                this.listingUpdate.error = '';
                this.listingUpdate.confirmLocal = false;
                this._listingUpdateSnapshot = null;
                var dialog = this.$refs && this.$refs.listingUpdateDialog;
                if (dialog && dialog.open) dialog.close();
            },
            resetListingUpdateOutcome: function () {
                if (this.listingUpdate.phase === 'applying') return false;
                if (this._listingUpdateAbort) {
                    this._listingUpdateAbort.abort();
                    this._listingUpdateAbort = null;
                    this._listingUpdateRevision = (this._listingUpdateRevision || 0) + 1;
                    this.clearListingUpdateTimeout();
                }
                this.listingUpdate.phase = 'idle';
                this.listingUpdate.preview = null;
                this.listingUpdate.report = null;
                this.listingUpdate.editorUrl = null;
                this.listingUpdate.error = '';
                this.listingUpdate.confirmLocal = false;
                this._listingUpdateSnapshot = null;
                return true;
            },
            listingUpdateCandidateId: function (group) {
                if (!this.listingUpdateScopeEligible || !group ||
                    !Array.isArray(group.listings) || group.listings.length !== 1 ||
                    (group.listing_count !== undefined && group.listing_count !== 1)) return null;
                var listing = group._p;
                var accountId = Number(this.filters.account_id);
                return listing && listing.marketplace_code === 'ozon' &&
                    listing.account_id === accountId &&
                    Number.isSafeInteger(listing.id) && listing.id > 0
                    ? listing.id : null;
            },
            listingUpdateSelected: function (group) {
                var id = this.listingUpdateCandidateId(group);
                return id !== null && this.listingUpdate.selectedIds.includes(id);
            },
            toggleListingUpdate: function (group, checked) {
                var id = this.listingUpdateCandidateId(group);
                if (id === null) return false;
                if (this.listingUpdate.selectedIds.length &&
                    this.listingUpdate.selectionScopeKey !== this.listingUpdateScopeKey()) {
                    this.clearListingUpdateSelection();
                }
                var ids = this.listingUpdate.selectedIds.slice();
                var index = ids.indexOf(id);
                if (checked && index === -1) {
                    if (ids.length >= LISTING_UPDATE_MAX) {
                        this.listingUpdate.error = 'За один раз можно выбрать не больше 200 карточек. Снимите отметку или сузьте поиск.';
                        return false;
                    }
                    ids.push(id);
                } else if (!checked && index !== -1) {
                    ids.splice(index, 1);
                }
                if (!sameOrderedIds(ids, this.listingUpdate.selectedIds)) {
                    if (!this.resetListingUpdateOutcome()) return false;
                }
                this.listingUpdate.selectedIds = ids;
                this.listingUpdate.error = '';
                this.persistListingUpdateSelection();
                return true;
            },
            toggleLoadedListingUpdates: function (checked) {
                var visible = this.visibleListingUpdateIds;
                if (!visible.length) return false;
                if (this.listingUpdate.selectedIds.length &&
                    this.listingUpdate.selectionScopeKey !== this.listingUpdateScopeKey()) {
                    this.clearListingUpdateSelection();
                }
                var current = this.listingUpdate.selectedIds.slice();
                var visibleSet = new Set(visible);
                if (!checked) {
                    current = current.filter(function (id) { return !visibleSet.has(id); });
                } else {
                    var combined = current.slice();
                    visible.forEach(function (id) {
                        if (!combined.includes(id)) combined.push(id);
                    });
                    if (combined.length > LISTING_UPDATE_MAX) {
                        this.listingUpdate.error = 'Загружено больше 200 карточек. Выберите часть вручную или сузьте поиск; набор не сокращён.';
                        return false;
                    }
                    current = combined;
                }
                if (!sameOrderedIds(current, this.listingUpdate.selectedIds)) {
                    if (!this.resetListingUpdateOutcome()) return false;
                }
                this.listingUpdate.selectedIds = current;
                this.listingUpdate.error = '';
                this.persistListingUpdateSelection();
                return true;
            },
            openListingUpdateDialog: function (event) {
                var dialog = this.$refs && this.$refs.listingUpdateDialog;
                if (!dialog) return false;
                this._listingUpdateTrigger = event && event.currentTarget
                    ? event.currentTarget : document.activeElement;
                if (!dialog.open) dialog.showModal();
                return true;
            },
            startListingUpdatePreview: function (event) {
                if (!this.listingUpdateCanPreview) return false;
                if (this.listingUpdate.phase === 'complete' && this.listingUpdate.report) {
                    return this.openListingUpdateDialog(event);
                }
                if (!this.openListingUpdateDialog(event)) return false;
                return this.readListingUpdatePreview();
            },
            cancelListingUpdateDialog: function () {
                if (this.listingUpdate.phase === 'applying') return false;
                if (this.listingUpdate.phase === 'previewing' && this._listingUpdateAbort) {
                    this._listingUpdateAbort.abort();
                    this._listingUpdateAbort = null;
                    this._listingUpdateRevision = (this._listingUpdateRevision || 0) + 1;
                    this.clearListingUpdateTimeout();
                    this.listingUpdate.phase = 'idle';
                    this.listingUpdate.preview = null;
                    this._listingUpdateSnapshot = null;
                }
                if (this.listingUpdate.phase === 'review') {
                    this.listingUpdate.phase = 'idle';
                    this.listingUpdate.preview = null;
                    this.listingUpdate.confirmLocal = false;
                    this._listingUpdateSnapshot = null;
                }
                var dialog = this.$refs && this.$refs.listingUpdateDialog;
                if (dialog && dialog.open) dialog.close();
                var trigger = this._listingUpdateTrigger;
                this.$nextTick(function () {
                    if (trigger && trigger.isConnected !== false && typeof trigger.focus === 'function') {
                        trigger.focus();
                    }
                });
                return true;
            },
            safeListingUpdateEndpoint: function (value) {
                try {
                    if (typeof value !== 'string' || !value) return null;
                    var target = new URL(value, window.location.origin);
                    if (target.origin !== window.location.origin || target.username || target.password || target.hash) return null;
                    return target.pathname + target.search;
                } catch (_) { return null; }
            },
            readListingUpdatePreview: async function () {
                var ids = this.listingUpdate.selectedIds.slice();
                var accountId = Number(this.filters.account_id);
                var accountLabel = this.accountLabel(accountId) || '';
                var scopeKey = this.listingUpdateScopeKey();
                if (!scopeKey || !accountLabel || !positiveIdList(ids, LISTING_UPDATE_MAX) ||
                    !this.listingUpdateScopeEligible || this.listingUpdate.selectionScopeKey !== scopeKey) {
                    this.listingUpdate.error = 'Выберите карточки одного магазина Ozon в режиме «По площадкам».';
                    this.listingUpdate.phase = 'read_failed';
                    return false;
                }
                var endpoint = this.safeListingUpdateEndpoint(this.urls.previewUpdateDrafts);
                var returnTo = this.catalogUrl(this.requestedPage || this.loadedStartPage || 1, this.urls.base);
                if (!endpoint || !this.safeCatalogReturnUrl(returnTo)) {
                    this.listingUpdate.error = 'Не удалось проверить адрес каталога. Обновите страницу и повторите выбор.';
                    this.listingUpdate.phase = 'read_failed';
                    return false;
                }
                if (this._listingUpdateAbort) this._listingUpdateAbort.abort();
                this.clearListingUpdateTimeout();
                var controller = new AbortController();
                var timedOut = false;
                var timeout = setTimeout(function () {
                    timedOut = true;
                    controller.abort();
                }, LISTING_UPDATE_PREVIEW_TIMEOUT_MS);
                this._listingUpdateTimeout = timeout;
                var revision = (this._listingUpdateRevision || 0) + 1;
                this._listingUpdateRevision = revision;
                this._listingUpdateAbort = controller;
                var snapshot = {
                    accountId: accountId,
                    accountLabel: accountLabel,
                    listingIds: ids,
                    scopeKey: scopeKey,
                    returnTo: returnTo,
                };
                this._listingUpdateSnapshot = snapshot;
                this.listingUpdate.phase = 'previewing';
                this.listingUpdate.error = '';
                this.listingUpdate.confirmLocal = false;
                this.listingUpdate.preview = null;
                this.listingUpdate.report = null;
                this.listingUpdate.editorUrl = null;
                try {
                    var response = await fetch(endpoint, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {
                            Accept: 'application/json',
                            'Content-Type': 'application/json',
                            'X-CSRFToken': CSRF,
                        },
                        body: JSON.stringify({
                            account_id: accountId,
                            listing_ids: ids,
                            return_to: returnTo,
                        }),
                        signal: controller.signal,
                    });
                    var data = await response.json();
                    if (timedOut) {
                        throw new Error(LISTING_UPDATE_PREVIEW_TIMEOUT_MESSAGE);
                    }
                    if (!response.ok || !data || data.success !== true ||
                        !validListingUpdatePreview(data.preview, snapshot)) {
                        throw new Error('Не удалось подтвердить точный список Ozon. Проверьте список ещё раз.');
                    }
                    if (controller.signal.aborted || this._listingUpdateRevision !== revision ||
                        this.listingUpdateScopeKey() !== scopeKey ||
                        !sameOrderedIds(this.listingUpdate.selectedIds, ids)) return false;
                    this.listingUpdate.preview = data.preview;
                    this.listingUpdate.phase = 'review';
                    this.$nextTick(() => {
                        if (this.$refs && this.$refs.listingUpdateConfirm &&
                            this.$refs.listingUpdateConfirm.isConnected !== false) {
                            this.$refs.listingUpdateConfirm.focus();
                        }
                    });
                    return true;
                } catch (error) {
                    if (error && error.name === 'AbortError' && !timedOut) return false;
                    if (this._listingUpdateRevision === revision) {
                        this.listingUpdate.phase = 'read_failed';
                        this.listingUpdate.preview = null;
                        this.listingUpdate.error = timedOut
                            ? LISTING_UPDATE_PREVIEW_TIMEOUT_MESSAGE
                            : error && typeof error.message === 'string'
                            ? error.message.slice(0, 300)
                            : 'Не удалось проверить список. Выбор сохранён; прочитайте список ещё раз.';
                    }
                    return false;
                } finally {
                    this.clearListingUpdateTimeout(timeout);
                    if (this._listingUpdateAbort === controller) this._listingUpdateAbort = null;
                }
            },
            safeCatalogReturnUrl: function (value) {
                try {
                    var target = new URL(value, window.location.origin);
                    var base = new URL(this.urls.base, window.location.origin);
                    var params = target.searchParams;
                    var allowed = ['marketplace', 'account_id', 'status', 'link_status',
                        'include_unavailable', 'search', 'page', 'per_page'];
                    if (target.origin !== window.location.origin || target.pathname !== base.pathname ||
                        target.username || target.password || target.hash ||
                        Array.from(params.keys()).some(function (key) { return !allowed.includes(key); }) ||
                        allowed.some(function (key) { return params.getAll(key).length > 1; })) return false;
                    var page = params.get('page') || '1';
                    var perPage = params.get('per_page') || String(this.perPage);
                    var search = params.get('search') || '';
                    var expectedPage = this.requestedPage || this.loadedStartPage || 1;
                    return params.get('marketplace') === 'ozon' &&
                        params.get('account_id') === String(Number(this.filters.account_id)) &&
                        /^[1-9][0-9]*$/.test(page) && Number(page) <= 90071992547409 &&
                        /^[1-9][0-9]*$/.test(perPage) && Number(perPage) <= 100 &&
                        Number(page) === Number(expectedPage) && Number(perPage) === Number(this.perPage) &&
                        (params.get('status') || '') === (this.filters.status || '') &&
                        (params.get('link_status') || '') === (this.filters.link_status || '') &&
                        (params.get('include_unavailable') === '1') === !!this.filters.include_unavailable &&
                        ['', 'active', 'moderation', 'creating', 'error', 'archived', 'inactive', 'unknown'].includes(params.get('status') || '') &&
                        ['', 'linked', 'unlinked', 'ambiguous'].includes(params.get('link_status') || '') &&
                        ['', '0', '1'].includes(params.get('include_unavailable') || '') &&
                        search === (this.filters.search || '') &&
                        search.length <= 200 && !/[\x00-\x1f\x7f]/.test(search);
                } catch (_) { return false; }
            },
            safeListingUpdateEditorUrl: function (value, report, snapshot) {
                var draftIds = report.rows.filter(function (row) {
                    return row.status === 'created' || row.status === 'existing';
                }).map(function (row) { return row.draft_id; });
                if (!draftIds.length) return value === null ? null : false;
                try {
                    if (typeof value !== 'string' || value.length > 4096) return false;
                    var target = new URL(value, window.location.origin);
                    var expected = new URL(this.urls.repairDraftSelection, window.location.origin);
                    if (target.origin !== window.location.origin || target.pathname !== expected.pathname ||
                        target.username || target.password || target.hash) return false;
                    var params = target.searchParams;
                    var allowed = ['account_id', 'draft_ids', 'return_to'];
                    if (Array.from(params.keys()).some(function (key) { return !allowed.includes(key); }) ||
                        params.getAll('account_id').length !== 1 ||
                        params.get('account_id') !== String(snapshot.accountId) ||
                        params.getAll('draft_ids').length !== 1 ||
                        params.getAll('return_to').length !== 1) return false;
                    var parsedDraftIds = params.get('draft_ids').split(',').map(function (item) {
                        return /^[1-9][0-9]*$/.test(item) ? Number(item) : NaN;
                    });
                    if (!positiveIdList(parsedDraftIds, LISTING_UPDATE_MAX) ||
                        !sameOrderedIds(parsedDraftIds, draftIds)) return false;
                    var returnUrl = new URL(params.get('return_to'), window.location.origin);
                    var expectedReturn = new URL(snapshot.returnTo, window.location.origin);
                    if (returnUrl.origin !== window.location.origin || returnUrl.username || returnUrl.password ||
                        returnUrl.hash || returnUrl.pathname !== expectedReturn.pathname ||
                        returnUrl.searchParams.toString() !== expectedReturn.searchParams.toString()) return false;
                    return target.pathname + target.search;
                } catch (_) { return false; }
            },
            applyListingUpdatePreview: async function () {
                var preview = this.listingUpdate.preview;
                var snapshot = this._listingUpdateSnapshot;
                if (this.listingUpdate.phase !== 'review' || !preview || !snapshot ||
                    !this.listingUpdate.confirmLocal ||
                    !sameOrderedIds(this.listingUpdate.selectedIds, snapshot.listingIds) ||
                    this.listingUpdateScopeKey() !== snapshot.scopeKey ||
                    !validListingUpdatePreview(preview, snapshot) ||
                    !preview.rows.some(function (row) { return row.status !== 'blocked'; })) return false;
                var endpoint = this.safeListingUpdateEndpoint(this.urls.applyUpdateDrafts);
                if (!endpoint) {
                    this.listingUpdate.phase = 'apply_uncertain';
                    this.listingUpdate.error = 'Не удалось проверить адрес подтверждения. Сначала перечитайте выбранный список.';
                    return false;
                }
                var controller = new AbortController();
                this.clearListingUpdateTimeout();
                var timedOut = false;
                var timeout = setTimeout(function () {
                    timedOut = true;
                    controller.abort();
                }, LISTING_UPDATE_APPLY_TIMEOUT_MS);
                this._listingUpdateTimeout = timeout;
                var revision = (this._listingUpdateRevision || 0) + 1;
                this._listingUpdateRevision = revision;
                this._listingUpdateAbort = controller;
                this.listingUpdate.phase = 'applying';
                this.listingUpdate.error = '';
                try {
                    var response = await fetch(endpoint, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {
                            Accept: 'application/json',
                            'Content-Type': 'application/json',
                            'X-CSRFToken': CSRF,
                        },
                        body: JSON.stringify({ token: preview.token, confirm_local: true }),
                        signal: controller.signal,
                    });
                    var data = await response.json();
                    if (timedOut) {
                        throw new Error(LISTING_UPDATE_APPLY_TIMEOUT_MESSAGE);
                    }
                    if (!response.ok || !data || data.success !== true ||
                        !validListingUpdateReport(data.report, snapshot)) {
                        throw new Error('Сервер не подтвердил итог для выбранных карточек. Перечитайте список перед новым подтверждением.');
                    }
                    var editorUrl = this.safeListingUpdateEditorUrl(data.report.editor_url, data.report, snapshot);
                    if (editorUrl === false) {
                        throw new Error('Ссылка на редактор не совпала с магазином и черновиками из итога. Перечитайте список.');
                    }
                    this.listingUpdate.report = data.report;
                    this.listingUpdate.editorUrl = editorUrl;
                    this.listingUpdate.phase = 'complete';
                    this.$nextTick(() => {
                        if (this.$refs && this.$refs.listingUpdateResultHeading &&
                            this.$refs.listingUpdateResultHeading.isConnected !== false) {
                            this.$refs.listingUpdateResultHeading.focus();
                        }
                    });
                    return true;
                } catch (error) {
                    if (this._listingUpdateRevision === revision) {
                        this.listingUpdate.phase = 'apply_uncertain';
                        this.listingUpdate.error = timedOut
                            ? LISTING_UPDATE_APPLY_TIMEOUT_MESSAGE
                            : error && typeof error.message === 'string'
                            ? error.message.slice(0, 300)
                            : 'Не удалось подтвердить результат. Перечитайте выбранный список перед новым подтверждением.';
                    }
                    return false;
                } finally {
                    this.clearListingUpdateTimeout(timeout);
                    if (this._listingUpdateAbort === controller) this._listingUpdateAbort = null;
                }
            },
            listingUpdateStatusLabel: function (status) {
                return ({ready: 'Готово к обновлению', existing: 'Черновик уже есть', blocked: 'Нельзя подготовить',
                    created: 'Черновик создан', error: 'Ошибка подготовки'})[status] || 'Статус не подтверждён';
            },
            listingUpdateRowCount: function (rows, status) {
                return Array.isArray(rows)
                    ? rows.filter(function (row) { return row && row.status === status; }).length : 0;
            },
            /* ---------- загрузка данных ---------- */
            catalogUrl: function (page, path) {
                var source;
                try {
                    source = new URL(this.catalogReturnUrl, location.origin);
                } catch (_) {
                    source = new URL('/marketplaces/listings/', location.origin);
                }
                var params = new URLSearchParams(source.search);
                var desired = this.scopeParams();
                if (this.filters.status) desired.set('status', this.filters.status);
                ['marketplace', 'account_id', 'status', 'link_status', 'include_unavailable', 'search'].forEach(function (key) {
                    if (desired.has(key)) params.set(key, desired.get(key));
                    else if (key === 'include_unavailable' && params.has(key)) params.set(key, '0');
                    else params.delete(key);
                });
                if (Number.isInteger(this.perPage) && this.perPage > 0 && this.perPage <= 100) {
                    params.set('per_page', String(this.perPage));
                }
                if (Number.isInteger(page) && page > 1) params.set('page', String(page));
                else if (params.has('page')) params.set('page', '1');
                var catalogPath = path || location.pathname;
                if (![this.urls.base, this.urls.base.slice(0, -1) + '/beta', this.urls.classic].includes(catalogPath)) {
                    catalogPath = source.pathname;
                }
                return catalogPath + (params.size ? '?' + params.toString() : '');
            },
            pageForGroup: function (index) {
                var safeIndex = Number.isInteger(index) && index >= 0 ? index : 0;
                return (this.loadedStartPage || 1) + Math.floor(safeIndex / this.perPage);
            },
            onPopState: function () {
                var current = new URL(window.location.href);
                if (![this.urls.base, this.urls.base.slice(0, -1) + '/beta'].includes(current.pathname)) return;
                var allowed = ['marketplace', 'account_id', 'status', 'link_status', 'include_unavailable', 'search', 'page', 'per_page'];
                var params = current.searchParams;
                if (Array.from(params.keys()).some(function (key) { return !allowed.includes(key); }) ||
                    allowed.some(function (key) { return params.getAll(key).length > 1; })) return;
                var page = Number(params.get('page') || 1);
                var perPage = Number(params.get('per_page') || bootstrap.perPage || 60);
                if (!Number.isSafeInteger(page) || page < 1 || page > 90071992547409 ||
                    !Number.isInteger(perPage) || perPage < 1 || perPage > 100 || current.hash || /\\/.test(current.search)) return;
                var marketplace = (params.get('marketplace') || '').trim().toLowerCase();
                var accountId = params.get('account_id') || '';
                var status = params.get('status') || '';
                var linkStatus = params.get('link_status') || '';
                var unavailable = params.get('include_unavailable') || '';
                var search = params.get('search') || '';
                var parsedAccountId = accountId && /^[0-9]+$/.test(accountId) ? Number(accountId) : null;
                if (!['', 'wb', 'ozon'].includes(marketplace) ||
                    (accountId && (!Number.isSafeInteger(parsedAccountId) || parsedAccountId < 1))) return;
                if (!['', 'active', 'moderation', 'creating', 'error', 'archived', 'inactive', 'unknown'].includes(status) ||
                    !['', 'linked', 'unlinked', 'ambiguous'].includes(linkStatus) ||
                    !['', '1', '0', 'true', 'false', 'on', 'off', 'yes', 'no'].includes(unavailable.toLowerCase()) ||
                    search.length > 200 || /[\x00-\x1f\x7f]/.test(search)) return;
                this.historyRestoring = true;
                this.filters = {
                    marketplace: marketplace || null,
                    account_id: parsedAccountId || null,
                    status: status,
                    link_status: linkStatus,
                    include_unavailable: ['1', 'true', 'yes', 'on'].includes(unavailable.toLowerCase()),
                    search: search
                };
                this.catalogReturnUrl = current.pathname + current.search;
                this.searchInput = this.filters.search;
                this.perPage = perPage;
                this.requestedPage = page;
                this.loadedStartPage = page;
                this.$nextTick(function () {
                    this.historyRestoring = false;
                    this.refresh();
                });
            },
            scopeParams: function () {
                var params = new URLSearchParams();
                if (this.filters.marketplace) params.set('marketplace', this.filters.marketplace);
                if (this.filters.account_id) params.set('account_id', this.filters.account_id);
                if (this.filters.link_status) params.set('link_status', this.filters.link_status);
                if (this.filters.include_unavailable) params.set('include_unavailable', '1');
                if (this.filters.search) params.set('search', this.filters.search);
                return params;
            },
            refresh: function () {
                this.fetchList(true);
                this.fetchFacets();
            },
            animateTotal: function (target) {
                var self = this;
                if (this._totalRaf) cancelAnimationFrame(this._totalRaf);
                var reduced = window.matchMedia
                    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
                if (reduced || !target) {
                    this.displayTotal = target;
                    return;
                }
                var from = this.displayTotal;
                var start = null;
                var duration = 450;
                var step = function (ts) {
                    if (start === null) start = ts;
                    var progress = Math.min(1, (ts - start) / duration);
                    var eased = 1 - Math.pow(1 - progress, 3);
                    self.displayTotal = Math.round(from + (target - from) * eased);
                    if (progress < 1) self._totalRaf = requestAnimationFrame(step);
                };
                this._totalRaf = requestAnimationFrame(step);
            },
            normalizeGroups: function (rows) {
                var self = this;
                var groups = this.mode === 'products'
                    ? (rows || [])
                    : (rows || []).map(function (listing) {
                        return {
                            key: 'l' + listing.id,
                            imported_product_id: listing.imported_product_id,
                            listing_count: 1,
                            listings: [listing]
                        };
                    });
                groups.forEach(function (group) {
                    var listings = group.listings || [];
                    var withPhoto = listings.filter(function (l) {
                        return l.primary_image && !self.imgFailed[l.id];
                    });
                    // Товар без единого живого фото остаётся в выдаче с плиткой-буквой
                    group._p = withPhoto[0] || listings[0] || null;
                    group._hidden = Math.max(
                        0,
                        (group.listing_count || listings.length) - listings.length
                    );
                });
                return groups.filter(function (group) { return !!group._p; });
            },
            fetchList: function (reset) {
                var self = this;
                if (this._listAbort) this._listAbort.abort();
                var abort = new AbortController();
                this._listAbort = abort;

                if (reset) { this.loading = true; this.error = null; } else { this.loadingMore = true; }
                var params = this.scopeParams();
                if (this.filters.status) params.set('status', this.filters.status);
                var endpoint = this.mode === 'products' ? this.urls.groups : this.urls.api;
                var scope = endpoint + '?' + params.toString();
                if (reset && this._loadedScope && this._loadedScope !== scope) {
                    if (this.drawer.open) this.closeDrawer();
                    this.items = [];
                    this.selectedIndex = -1;
                    this.pagination = {page: 1, pages: 1, total: 0, has_next: false};
                }
                var requestedPage = reset ? (this.requestedPage || 1) : (this.pagination.page + 1);
                if (reset) this.loadedStartPage = requestedPage;
                params.set('page', requestedPage);
                params.set('per_page', this.perPage);

                fetch(endpoint + '?' + params.toString(), {
                    headers: { Accept: 'application/json' },
                    signal: abort.signal
                }).then(S.readJson).then(function (data) {
                    if (abort.signal.aborted) return;
                    self._loadedScope = scope;
                    var groups = self.normalizeGroups(data.items);
                    if (reset) {
                        var selectedId = self.drawer.open && self.current ? self.current.id : null;
                        self.items = groups;
                        self.selectedIndex = selectedId === null ? -1 : groups.findIndex(group => group.listings.some(row => row.id === selectedId));
                        if (self.drawer.open) {
                            if (self.selectedIndex < 0) self.closeDrawer();
                            else self.drawer.memberIdx = self.activeGroup.listings.findIndex(row => row.id === selectedId);
                        }
                        self.animate = true;
                        setTimeout(function () { self.animate = false; }, 800);
                    } else {
                        var seen = {};
                        self.items.forEach(function (group) { seen[group.key] = true; });
                        groups.forEach(function (group) {
                            if (!seen[group.key]) self.items.push(group);
                        });
                    }
                    self.pagination = data.pagination || self.pagination;
                    self.requestedPage = self.pagination.page || requestedPage;
                    if (reset) self.loadedStartPage = self.pagination.page || requestedPage;
                }).catch(function (err) {
                    if (err && err.name === 'AbortError') return;
                    self.error = err instanceof TypeError ? 'Не удалось связаться с сервером. Проверьте соединение и повторите попытку.' : err.message || 'Не удалось загрузить каталог';
                }).finally(function () {
                    if (!abort.signal.aborted) { self.loading = false; self.loadingMore = false; }
                });
            },
            loadMore: function () { this.fetchList(false); },
            fetchFacets: function () {
                var self = this;
                if (this._facetAbort) this._facetAbort.abort();
                this.statusChips.forEach(chip => { this.counts[chip.key] = null; });
                this.channelCounts = {};
                var abort = new AbortController();
                this._facetAbort = abort;

                var params = this.scopeParams();
                if (this.filters.status) params.set('status', this.filters.status);
                fetch(this.urls.facets + '?' + params.toString(), {
                    headers: { Accept: 'application/json' },
                    signal: abort.signal
                }).then(S.readJson).then(function (data) {
                    if (abort.signal.aborted) return;
                    var statuses = data.statuses || {};
                    self.statusChips.forEach(function (chip) {
                        var key = chip.value || 'all';
                        self.counts[chip.key] = statuses[key] || 0;
                    });
                    self.channelCounts = data.channels || {};
                }).catch(function () { /* счётчики некритичны для работы каталога */ });
            },

            /* ---------- фильтры и вид ---------- */
            setChannel: function (key) {
                if (key === 'wb') {
                    this.filters.marketplace = 'wb';
                    this.filters.account_id = null;
                } else if (key === 'ozon') {
                    this.filters.marketplace = 'ozon';
                    this.filters.account_id = null;
                } else if (key.indexOf('ozon:') === 0) {
                    this.filters.marketplace = 'ozon';
                    this.filters.account_id = parseInt(key.slice(5), 10) || null;
                } else {
                    this.filters.marketplace = null;
                    this.filters.account_id = null;
                }
            },
            setStatus: function (value) { this.filters.status = value; },
            setView: function (value) {
                if (!['grid', 'table'].includes(value)) return;
                this.view = value;
                remember('sh-mcat-view', value);
            },
            setMode: function (value) {
                if (this.mode === value) return;
                // Иначе открытая карточка осталась бы на другом товаре из нового списка
                this.clearListingUpdateSelection();
                if (this.drawer.open) this.closeDrawer();
                this.mode = value;
                remember('sh-mcat-mode', value);
                this.fetchList(true);
            },
            resetFilters: function () {
                this.searchInput = '';
                this.filters.search = '';
                this.filters.status = '';
                this.filters.link_status = '';
                this.filters.include_unavailable = false;
            },

            /* ---------- quick view ---------- */
            openDrawer: function (index, memberIdx) {
                this.selectedIndex = index;
                this.drawer.memberIdx = memberIdx || 0;
                this.drawer.open = true;
                this.drawer.image = null;
                this.loadDetail();
                var self = this;
                this.$nextTick(function () {
                    if (self.$refs.drawerEl) self.$refs.drawerEl.focus();
                });
            },
            stepDrawer: function (delta) {
                var next = this.selectedIndex + delta;
                if (next < 0 || next >= this.items.length) return;
                this.selectedIndex = next;
                this.drawer.memberIdx = 0;
                this.drawer.image = null;
                this.drawer.link = null;
                this.loadDetail();
            },
            switchMember: function (memberIdx) {
                if (this.drawer.memberIdx === memberIdx) return;
                this.drawer.memberIdx = memberIdx;
                this.drawer.image = null;
                this.loadDetail();
            },
            closeDrawer: function () {
                if (this._detailAbort) this._detailAbort.abort();
                this.drawer.open = false;
                this.drawer.detail = null;
                this.drawer.error = null;
                this.drawer.image = null;
                this.drawer.link = null;
                var self = this;
                this.$nextTick(function () { self.focusSelected(false); });
            },
            loadDetail: function () {
                var self = this;
                var item = this.current;
                if (!item) return;
                if (this._detailAbort) this._detailAbort.abort();
                var abort = new AbortController();
                this._detailAbort = abort;
                this.drawer.loading = true;
                this.drawer.error = null;
                fetch(this.urls.base + item.id, {
                    headers: { Accept: 'application/json' }, signal: abort.signal
                }).then(S.readJson).then(function (data) {
                    if (!abort.signal.aborted && self.drawer.open && self.current && data.listing && data.listing.id === self.current.id) {
                        self.drawer.detail = data.listing;
                        self.drawer.link = data.product_link || null;
                    }
                }).catch(function (err) {
                    if (abort.signal.aborted || !self.drawer.open) return;
                    self.drawer.error = err instanceof TypeError ? 'Не удалось связаться с сервером. Откройте карточку ещё раз.' : err.message || 'Не удалось загрузить детали';
                }).finally(function () {
                    if (!abort.signal.aborted) self.drawer.loading = false;
                });
            },

            /* ---------- клавиатура ---------- */
            onKey: function (event) {
                if (event.defaultPrevented) return;
                var target = event.target;
                var isField = target && (/^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName) || target.isContentEditable);
                if (this.drawer.open) {
                    if (event.key === 'Escape') { event.preventDefault(); this.closeDrawer(); }
                    if (event.key === 'ArrowRight' && !isField) { event.preventDefault(); this.stepDrawer(1); }
                    if (event.key === 'ArrowLeft' && !isField) { event.preventDefault(); this.stepDrawer(-1); }
                    if (event.key === 'Tab') this.trapFocus(event);
                    return;
                }
                if (isField) return;
                if (event.key === '/') {
                    event.preventDefault();
                    if (this.$refs.search) this.$refs.search.focus();
                    return;
                }
                if (target && target.closest && target.closest('a,button')) return;
                if (!this.items.length) return;
                var cols = this.view === 'grid' ? this.gridColumns() : 1;
                var moves = {
                    ArrowRight: 1,
                    ArrowLeft: -1,
                    ArrowDown: cols,
                    ArrowUp: -cols
                };
                if (event.key in moves) {
                    event.preventDefault();
                    var next = this.selectedIndex + moves[event.key];
                    next = Math.max(0, Math.min(this.items.length - 1, next));
                    this.selectedIndex = next;
                    this.focusSelected(true);
                } else if (event.key === 'Enter' && this.selectedIndex >= 0) {
                    event.preventDefault();
                    this.openDrawer(this.selectedIndex);
                }
            },
            gridColumns: function () {
                var grid = this.$refs.grid;
                if (!grid) return 1;
                var style = window.getComputedStyle(grid).gridTemplateColumns;
                return style ? style.split(' ').length : 1;
            },
            trapFocus: function (event) {
                // Модалка удерживает фокус: Tab не должен уводить на фон под backdrop
                var root = this.$refs.drawerEl;
                if (!root) return;
                var focusable = root.querySelectorAll(
                    'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])'
                );
                if (!focusable.length) return;
                var first = focusable[0];
                var last = focusable[focusable.length - 1];
                var active = document.activeElement;
                if (!root.contains(active)) {
                    event.preventDefault();
                    (event.shiftKey ? last : first).focus();
                    return;
                }
                if (event.shiftKey && active === first) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && active === last) {
                    event.preventDefault();
                    first.focus();
                }
            },
            focusSelected: function (scroll) {
                var grid = this.view === 'grid' ? this.$refs.grid : this.$refs.rows;
                if (!grid || this.selectedIndex < 0) return;
                var card = grid.children[this.selectedIndex];
                if (!card) return;
                if (scroll) card.scrollIntoView({ block: 'nearest' });
                card.focus({ preventScroll: true });
            },

            /* ---------- синхронизация Ozon ---------- */
            canSync: function (acc) {
                var expiry = utcDate(acc.credential_expires_at);
                return this.ozonEnabled && acc.is_active && acc.has_credentials && acc.connection_status === 'connected'
                    && (!expiry || expiry > new Date());
            },
            accountSettingsUrl: function (acc) {
                return this.urls.accountsPage + '?account_id=' + encodeURIComponent(acc.id) + '#ozon-account-' + acc.id;
            },
            accountState: function (acc) {
                if (!acc.is_active || !acc.has_credentials) return {label: 'Отключён', tone: 'muted'};
                var expiry = utcDate(acc.credential_expires_at);
                if (expiry && expiry <= new Date()) return {label: 'Ключ истёк', tone: 'warn'};
                if (this.canSync(acc)) return {label: 'Подключён', tone: 'ok'};
                return {label: acc.connection_status === 'invalid' ? 'Ключ отклонён' : 'Нужна проверка', tone: 'warn'};
            },
            syncFailed: function (acc) {
                return !!(acc._error || (acc.sync_job || {}).status === 'failed' || (acc.last_sync || {}).status === 'failed');
            },
            syncCooldownAt: function (acc) {
                var job = acc && acc.sync_job;
                if (!job || job.status !== 'failed') return null;
                var retry = utcDate(job.manual_retry_at);
                if (!retry || Number.isNaN(retry.getTime())) return null;
                // Track cooldownClock so the disabled controls become eligible
                // when the local deadline passes, without creating a request.
                var now = Math.max(Date.now(), this.cooldownClock || 0);
                return retry.getTime() > now ? retry.toISOString() : null;
            },
            syncCoolingDown: function (acc) {
                return !!this.syncCooldownAt(acc);
            },
            formattedSyncCooldownAt: function (acc) {
                var value = utcDate(this.syncCooldownAt(acc));
                return value && !Number.isNaN(value.getTime())
                    ? value.toLocaleString('ru-RU', {
                        day:'2-digit', month:'2-digit', year:'numeric',
                        hour:'2-digit', minute:'2-digit', timeZone:'UTC'
                    }) + ' UTC' : '';
            },
            syncDate: function (acc) {
                var retry = utcDate((acc.sync_job || {}).next_retry_at);
                if (this.syncCoolingDown(acc)) return '';
                return this.syncActive(acc) && retry
                    ? 'Продолжим после ' + retry.toLocaleString('ru-RU', {
                        day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'
                    }) : '';
            },
            scheduleCooldownWake: function () {
                clearTimeout(cooldownWakeTimer);
                if (syncStopped || document.hidden) return;
                var now = Date.now();
                var next = this.accounts.reduce(function (soonest, acc) {
                    var retry = acc.sync_job && acc.sync_job.status === 'failed'
                        ? utcDate(acc.sync_job.manual_retry_at) : null;
                    if (!retry || Number.isNaN(retry.getTime()) || retry.getTime() <= now) return soonest;
                    return soonest === null ? retry.getTime() : Math.min(soonest, retry.getTime());
                }, null);
                if (next === null) return;
                cooldownWakeTimer = setTimeout(() => {
                    if (syncStopped) return;
                    this.cooldownClock = Date.now();
                    this.scheduleCooldownWake();
                }, Math.min(2147483647, Math.max(0, next - now) + 10));
            },
            lastCompletedAt: function (acc) {
                var value = utcDate(acc.last_completed_at);
                return value && !Number.isNaN(value.getTime()) ? acc.last_completed_at : null;
            },
            formattedLastCompletedAt: function (acc) {
                var value = utcDate(this.lastCompletedAt(acc));
                return value && !Number.isNaN(value.getTime()) ? value.toLocaleString('ru-RU', {
                    day: '2-digit', month: '2-digit', year: 'numeric',
                    hour: '2-digit', minute: '2-digit', timeZone: 'UTC'
                }) + ' UTC' : '';
            },
            syncActive: function (acc) {
                return acc._syncing || activeJob(acc.sync_job);
            },
            syncActionLabel: function (acc) {
                return this.syncActive(acc) ? 'загружается' : 'обновить';
            },
            syncTone: function (acc) {
                if (this.syncActive(acc)) return 'mcat-sync-pill--running';
                var status = acc.last_sync && acc.last_sync.status;
                if (acc._error || (acc.sync_job || {}).status === 'failed' || status === 'failed') return 'mcat-sync-pill--failed';
                return '';
            },
            syncHint: function (acc) {
                if (acc._error) return acc._error;
                if (!this.canSync(acc) && !this.syncActive(acc)) return 'Проверьте подключение в настройках магазина. Сохранённые товары остаются доступны.';
                if (acc.sync_job) return (acc.sync_job.message || 'Загрузка каталога')
                    + (acc.sync_job.processed ? ' · товаров ' + acc.sync_job.processed : '');
                var sync = acc.last_sync;
                if (!sync) return 'Каталог ещё не синхронизировался';
                if (sync.status === 'completed') return 'Загружено товаров: ' + new Intl.NumberFormat('ru-RU').format(sync.seen_count || 0) + '. Можно работать с каталогом.';
                var parts = ['Загрузка: ' + (SYNC_STATUS_RU[sync.status] || sync.status)];
                if (sync.page_count) parts.push('страниц ' + sync.page_count);
                if (sync.seen_count) parts.push('товаров ' + sync.seen_count);
                if (sync.error_message) parts.push(sync.error_message);
                return parts.join(' · ');
            },
            scheduleSyncPoll: function () {
                clearTimeout(syncTimer);
                if (syncStopped || document.hidden || !this.ozonEnabled || this.syncSessionExpired
                    || !this.accounts.some(acc => activeJob(acc.sync_job))) return;
                syncTimer = setTimeout(() => this.pollSync(), Math.min(30000, 5000 * (1 + this.syncFailures)));
            },
            pollSync: async function () {
                if (syncStopped || document.hidden || syncController) return;
                syncController = new AbortController();
                var revision = syncRevision;
                var timeout = setTimeout(() => syncController && syncController.abort(), 15000);
                try {
                    var data = await fetch(this.urls.accountsStatus, {
                        credentials: 'same-origin', cache: 'no-store',
                        headers: {Accept: 'application/json'}, signal: syncController.signal
                    }).then(S.readJson);
                    if (syncStopped || revision !== syncRevision) return;
                    var changed = false;
                    this.accounts.forEach(acc => {
                        var job = (data.onboarding_jobs || {})[acc.id];
                        if (activeJob(acc.sync_job) && job && job.status === 'completed') changed = true;
                        acc.sync_job = job || null;
                        acc.last_sync = (data.catalog_syncs || {})[acc.id] || acc.last_sync;
                        if (data.catalog_completed_at && typeof data.catalog_completed_at === 'object') {
                            acc.last_completed_at = data.catalog_completed_at[acc.id] || null;
                        }
                        var current = (data.accounts || []).find(row => row.id === acc.id);
                        if (current) {
                            acc.is_active = current.is_active;
                            acc.has_credentials = current.has_credentials;
                            acc.connection_status = current.connection_status;
                            acc.credential_expires_at = current.credential_expires_at;
                        }
                    });
                    if (typeof data.ozon_enabled === 'boolean') this.ozonEnabled = data.ozon_enabled;
                    this.syncError = '';
                    this.syncFailures = 0;
                    if (changed) this.refresh();
                } catch (error) {
                    if (!syncStopped && !document.hidden) {
                        this.syncSessionExpired = error.code === 'auth_required';
                        this.syncError = this.syncSessionExpired ? error.message : 'Не удалось обновить статус. Фоновая загрузка не отменена.';
                        this.syncFailures += 1;
                    }
                } finally {
                    clearTimeout(timeout);
                    syncController = null;
                    this.scheduleCooldownWake();
                    this.scheduleSyncPoll();
                }
            },
            syncAccount: function (acc, forceRestart) {
                var self = this;
                this.cooldownClock = Date.now();
                this.scheduleCooldownWake();
                if (this.syncActive(acc) || this.syncCoolingDown(acc) || !this.canSync(acc)) return;
                if (forceRestart && !window.confirm('Начать синхронизацию ' + acc.label + ' заново с первой страницы?')) return;
                acc._syncing = true;
                acc._error = null;
                syncRevision += 1;
                var controller = new AbortController();
                syncRequests.add(controller);
                var timeout = setTimeout(() => controller.abort(), 20000);
                var body = {};
                if (forceRestart) body.force_restart = true;
                fetch(this.urls.base + 'accounts/' + acc.id + '/sync', {
                    method: 'POST',
                    credentials: 'same-origin', signal: controller.signal,
                    headers: {
                        'Content-Type': 'application/json',
                        Accept: 'application/json',
                        'X-CSRFToken': CSRF
                    },
                    body: JSON.stringify(body)
                }).then(async function (response) {
                    if (response.status === 409 && typeof response.clone === 'function') {
                        var failure = null;
                        try { failure = await response.clone().json(); } catch (_) { /* Preserve normal error handling. */ }
                        if (failure && failure.code === 'ozon_catalog_cooldown') {
                            await self.pollSync();
                            var cooldownError = new Error(failure.error || 'Ozon попросил паузу. Статус обновлён.');
                            cooldownError.code = 'ozon_catalog_cooldown';
                            throw cooldownError;
                        }
                    }
                    return S.readJson(response);
                }).then(function (data) {
                    if (syncStopped) return;
                    syncRevision += 1;
                    acc.sync_job = data.job;
                    self.scheduleCooldownWake();
                }).catch(function (err) {
                    if (err.code === 'ozon_catalog_cooldown') {
                        acc._error = self.syncCoolingDown(acc) ? null : err.message;
                        return;
                    }
                    acc._error = err.name === 'AbortError' || err instanceof TypeError
                        ? 'Ответ не получен. Загрузка могла начаться — обновите страницу перед повтором.'
                        : err.message || 'Не удалось запустить загрузку';
                }).finally(function () {
                    clearTimeout(timeout);
                    syncRequests.delete(controller);
                    acc._syncing = false;
                    self.scheduleSyncPoll();
                });
            },

            /* ---------- отображение (общий слой mcatShared) ---------- */
            plural: function (n, one, few, many) { return S.plural(n, one, few, many); },
            fmtMoney: function (value, currency) { return S.fmtMoney(value, currency); },
            priceOf: function (item) { return S.priceLabel(item); },
            basePriceOf: function (item) { var facts = S.priceFacts(item); return S.fmtMoney(facts.base, facts.currency) || '—'; },
            oldPriceOf: function (item) { return S.oldPriceLabel(item); },
            minPriceOf: function (item) { return S.minPriceLabel(item); },
            stockNum: function (item) { return S.stockNumber(item); },
            stockOf: function (item) { return S.stockLabel(item); },
            stockRows: function () { return S.stockRows(this.current); },
            statusMeta: function (item) { return S.statusMeta(item); },
            badgeOf: function (item) {
                if (!item.is_available) return { label: 'нет в синке', tone: 'warn' };
                if (item.normalized_status === 'active') return null;
                return S.statusMeta(item);
            },
            linkLabel: function (item) { return S.linkLabel(item); },
            providerStatusMeta: function (listing) { return S.providerStatusMeta(listing); },
            visibilityMeta: function (listing) { return S.visibilityMeta(listing); },
            linkSourceOf: function (listing) { return S.linkSourceLabel(listing.link_source); },
            groupBadge: function (group) {
                var listings = group.listings || [];
                if (listings.length === 1) return this.badgeOf(listings[0]);
                if (listings.some(function (l) { return !l.is_available; })) {
                    return { label: 'нет в синке', tone: 'warn' };
                }
                var statuses = listings.map(function (l) { return l.normalized_status; });
                if (statuses.indexOf('error') !== -1) return { label: 'Ошибка', tone: 'danger' };
                if (statuses.indexOf('moderation') !== -1 || statuses.indexOf('creating') !== -1) {
                    return { label: 'Модерация', tone: 'info' };
                }
                var terminal = statuses.every(function (s) {
                    return s === 'archived' || s === 'inactive';
                });
                if (terminal) return { label: 'Архив', tone: 'muted' };
                return null;
            },
            railClass: function (group) {
                var codes = {};
                (group.listings || []).forEach(function (l) {
                    codes[l.marketplace_code || 'wb'] = true;
                });
                if (codes.wb && codes.ozon) return 'mcat-rail--multi';
                return 'mcat-rail--' + (codes.ozon ? 'ozon' : 'wb');
            },
            chanLabel: function (listing) {
                if (listing.marketplace_code === 'wb') return 'WB';
                if (this.accounts.length > 1 && listing.account_label) {
                    return listing.account_label.length > 10
                        ? listing.account_label.slice(0, 9) + '…'
                        : listing.account_label;
                }
                return 'Ozon';
            },
            betaDetailUrl: function (listing, groupIndex) {
                if (!listing) return this.urls.base;
                var index = Number.isInteger(groupIndex) ? groupIndex : this.selectedIndex;
                var returnUrl = this.catalogUrl(this.pageForGroup(index));
                return this.urls.overviewBase + listing.id + '?return_to=' + encodeURIComponent(returnUrl);
            },
            letterOf: function (item) { return S.letterOf(item); },
            hoverItem: function (item) {
                if (item.hover_image && !this.hovered[item.id]) {
                    this.hovered[item.id] = true;
                }
            },
            discountOf: function (item) { return S.discountPercent(item); },
            fallbackClass: function (item) { return S.fallbackClass(item); },
            accountLabel: function (accountId) {
                for (var i = 0; i < this.accounts.length; i++) {
                    if (this.accounts[i].id === accountId) return this.accounts[i].label;
                }
                return null;
            },
            accountTabLabel: function (acc) {
                return this.accounts.length > 1 ? 'Ozon · ' + acc.label : 'Ozon';
            },
            channelShort: function (item) {
                if (item.marketplace_code === 'wb') return 'WB';
                if (this.accounts.length > 1 && item.account_label) {
                    return item.account_label.length > 12
                        ? item.account_label.slice(0, 11) + '…'
                        : item.account_label;
                }
                return 'Ozon';
            },
            channelFull: function (item) {
                if (item.marketplace_code === 'wb') return 'Wildberries';
                return item.account_label ? 'Ozon · ' + item.account_label : 'Ozon';
            },
            relTime: function (iso) { return S.relTime(iso); }
        }
    }).mount('#marketplace-catalog-app');
    var fallback = document.getElementById('mcat-bootstrap-fallback');
    if (fallback) fallback.remove();
})();
