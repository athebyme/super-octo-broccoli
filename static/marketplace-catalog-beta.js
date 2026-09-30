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
    var syncTimer, syncController, syncRevision = 0, syncStopped = false;
    var syncRequests = new Set();
    function activeJob(job) { return !!job && ['pending', 'running'].includes(job.status); }
    function saved(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
    function remember(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* Optional preference. */ } }
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
                accounts: (bootstrap.accounts || []).map(function (acc) {
                    acc._syncing = false;
                    acc._error = null;
                    return acc;
                }),
                urls: bootstrap.urls || {},
                illustration: bootstrap.illustration || '',
                perPage: bootstrap.perPage || 60,

                items: [],
                pagination: { page: 1, pages: 1, total: 0, has_next: false },
                loading: true,
                loadingMore: false,
                syncError: '', syncFailures: 0, syncSessionExpired: false,
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
                var params = this.scopeParams();
                if (this.filters.status) params.set('status', this.filters.status);
                return this.urls.classic + (params.size ? '?' + params.toString() : '');
            },
            channelKey: function () {
                if (this.filters.marketplace === 'wb') return 'wb';
                if (this.filters.marketplace === 'ozon') {
                    return this.filters.account_id ? 'ozon:' + this.filters.account_id : 'ozon';
                }
                return 'all';
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
                this._searchTimer = setTimeout(function () {
                    var next = (value || '').trim().slice(0, 200);
                    if (next !== self.filters.search) self.filters.search = next;
                }, 300);
            },
            filters: {
                deep: true,
                handler: function () {
                    var params = this.scopeParams();
                    if (this.filters.status) params.set('status', this.filters.status);
                    history.replaceState(null, '', location.pathname + (params.size ? '?' + params.toString() : ''));
                    this.refresh();
                }
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
            window.addEventListener('keydown', this._onKey);
            this._onVisibility = () => {
                clearTimeout(syncTimer);
                if (document.hidden) {
                    if (syncController) syncController.abort();
                } else { this.scheduleSyncPoll(); }
            };
            document.addEventListener('visibilitychange', this._onVisibility);
            this.scheduleSyncPoll();
            this.refresh();
        },
        beforeUnmount: function () {
            syncStopped = true;
            clearTimeout(syncTimer);
            clearTimeout(this._searchTimer);
            if (this._totalRaf) cancelAnimationFrame(this._totalRaf);
            if (syncController) syncController.abort();
            if (this._listAbort) this._listAbort.abort();
            if (this._facetAbort) this._facetAbort.abort();
            if (this._detailAbort) this._detailAbort.abort();
            syncRequests.forEach(controller => controller.abort());
            document.removeEventListener('visibilitychange', this._onVisibility);
            window.removeEventListener('keydown', this._onKey);
            document.body.style.overflow = '';
        },

        methods: {
            /* ---------- загрузка данных ---------- */
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
                params.set('page', reset ? 1 : (this.pagination.page + 1));
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
            syncDate: function (acc) {
                var retry = utcDate((acc.sync_job || {}).next_retry_at);
                var completed = (acc.last_sync || {}).status === 'completed' && utcDate(acc.last_sync.completed_at);
                var value = this.syncActive(acc) && retry ? retry : completed;
                return value ? (this.syncActive(acc) && retry ? 'Продолжим после ' : 'Полная загрузка: ')
                    + value.toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'}) : '';
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
                    this.scheduleSyncPoll();
                }
            },
            syncAccount: function (acc, forceRestart) {
                var self = this;
                if (this.syncActive(acc) || !this.canSync(acc)) return;
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
                }).then(S.readJson).then(function (data) {
                    if (syncStopped) return;
                    syncRevision += 1;
                    acc.sync_job = data.job;
                }).catch(function (err) {
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
            betaDetailUrl: function (listing) {
                return this.urls.base + 'view/' + listing.id;
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
