/* Каталог маркетплейсов · beta — Vue 3 (CDN, без бандлера).
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

    Vue.createApp({
        data: function () {
            return {
                ozonEnabled: !!bootstrap.ozonEnabled,
                accounts: (bootstrap.accounts || []).map(function (acc) {
                    acc._syncing = false;
                    acc._error = null;
                    return acc;
                }),
                urls: bootstrap.urls || {},
                perPage: bootstrap.perPage || 60,

                items: [],
                pagination: { page: 1, pages: 1, total: 0, has_next: false },
                loading: true,
                loadingMore: false,
                error: null,
                animate: false,

                filters: {
                    marketplace: null,
                    account_id: null,
                    status: '',
                    link_status: '',
                    include_unavailable: false,
                    search: ''
                },
                searchInput: '',
                counts: { all: null, active: null, moderation: null, error: null, archived: null },
                channelCounts: {},
                displayTotal: 0,

                view: localStorage.getItem('sh-mcat-view') === 'table' ? 'table' : 'grid',
                mode: localStorage.getItem('sh-mcat-mode') === 'listings' ? 'listings' : 'products',
                selectedIndex: -1,
                imgFailed: {},
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
                if (this.drawer.image) return this.drawer.image;
                if (this.current && this.current.primary_image && !this.imgFailed[this.current.id]) {
                    return this.current.primary_image;
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
                return urls.slice(0, 8);
            },
            syncableAccount: function () {
                var self = this;
                return this.accounts.filter(function (acc) {
                    return self.canSync(acc);
                })[0] || null;
            },
            hasActiveFilters: function () {
                return !!(this.filters.status || this.filters.link_status ||
                    this.filters.include_unavailable || this.filters.search ||
                    this.filters.marketplace || this.filters.account_id);
            },
            emptyState: function () {
                if (this.hasActiveFilters) {
                    return {
                        title: 'По этим фильтрам ничего не нашлось',
                        text: 'Попробуйте убрать часть условий или изменить запрос.',
                        action: 'reset'
                    };
                }
                if (this.ozonEnabled && !this.accounts.length) {
                    return {
                        title: 'Каталог пока пуст',
                        text: 'Подключите кабинет Ozon, чтобы его карточки появились здесь. Карточки Wildberries подтянутся после синхронизации товаров.',
                        action: 'accounts'
                    };
                }
                if (this.ozonEnabled) {
                    return {
                        title: 'Каталог пока пуст',
                        text: 'Запустите синхронизацию кабинета Ozon — карточки появятся здесь через пару минут.',
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
                handler: function () { this.refresh(); }
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
            this.refresh();
        },
        beforeUnmount: function () {
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
                params.set('page', reset ? 1 : (this.pagination.page + 1));
                params.set('per_page', this.perPage);
                var endpoint = this.mode === 'products' ? this.urls.groups : this.urls.api;

                fetch(endpoint + '?' + params.toString(), {
                    headers: { Accept: 'application/json' },
                    signal: abort.signal
                }).then(S.readJson).then(function (data) {
                    if (abort.signal.aborted) return;
                    var groups = self.normalizeGroups(data.items);
                    if (reset) {
                        self.items = groups;
                        self.selectedIndex = self.items.length ? 0 : -1;
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
                    self.error = err.message || 'Не удалось загрузить каталог';
                }).finally(function () {
                    if (!abort.signal.aborted) { self.loading = false; self.loadingMore = false; }
                });
            },
            loadMore: function () { this.fetchList(false); },
            fetchFacets: function () {
                var self = this;
                if (this._facetAbort) this._facetAbort.abort();
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
                this.view = value;
                localStorage.setItem('sh-mcat-view', value);
            },
            setMode: function (value) {
                if (this.mode === value) return;
                // Иначе открытая карточка осталась бы на другом товаре из нового списка
                if (this.drawer.open) this.closeDrawer();
                this.mode = value;
                localStorage.setItem('sh-mcat-mode', value);
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
                this.drawer.loading = true;
                this.drawer.error = null;
                fetch(this.urls.base + item.id, {
                    headers: { Accept: 'application/json' }
                }).then(S.readJson).then(function (data) {
                    if (self.current && data.listing && data.listing.id === self.current.id) {
                        self.drawer.detail = data.listing;
                        self.drawer.link = data.product_link || null;
                    }
                }).catch(function (err) {
                    self.drawer.error = err.message || 'Не удалось загрузить детали';
                }).finally(function () {
                    self.drawer.loading = false;
                });
            },

            /* ---------- клавиатура ---------- */
            onKey: function (event) {
                var target = event.target;
                var isField = target && /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName);
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
                return this.ozonEnabled && acc.is_active && acc.connection_status === 'connected';
            },
            syncActionLabel: function (acc) {
                var status = acc.last_sync && acc.last_sync.status;
                return status === 'paused' || status === 'running' ? 'продолжить' : 'синк';
            },
            syncTone: function (acc) {
                var status = acc.last_sync && acc.last_sync.status;
                if (acc._error || status === 'failed') return 'mcat-sync-pill--failed';
                if (status === 'running' || status === 'paused' || acc._syncing) return 'mcat-sync-pill--running';
                return '';
            },
            syncHint: function (acc) {
                if (acc._error) return acc._error;
                var sync = acc.last_sync;
                if (!sync) return 'Каталог ещё не синхронизировался';
                var parts = ['Синк: ' + (SYNC_STATUS_RU[sync.status] || sync.status)];
                if (sync.page_count) parts.push('страниц ' + sync.page_count);
                if (sync.seen_count) parts.push('товаров ' + sync.seen_count);
                if (sync.error_message) parts.push(sync.error_message);
                return parts.join(' · ');
            },
            syncAccount: function (acc, forceRestart) {
                var self = this;
                if (acc._syncing) return;
                if (forceRestart && !window.confirm('Начать синхронизацию ' + acc.label + ' заново с первой страницы?')) return;
                acc._syncing = true;
                acc._error = null;
                var body = { max_pages: 5 };
                if (forceRestart) body.force_restart = true;
                fetch(this.urls.base + 'accounts/' + acc.id + '/sync', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        Accept: 'application/json',
                        'X-CSRFToken': CSRF
                    },
                    body: JSON.stringify(body)
                }).then(S.readJson).then(function (data) {
                    acc.last_sync = data.sync || acc.last_sync;
                    self.refresh();
                }).catch(function (err) {
                    acc._error = err.message || 'Синхронизация не удалась';
                }).finally(function () {
                    acc._syncing = false;
                });
            },

            /* ---------- отображение (общий слой mcatShared) ---------- */
            plural: function (n, one, few, many) { return S.plural(n, one, few, many); },
            fmtMoney: function (value, currency) { return S.fmtMoney(value, currency); },
            priceOf: function (item) { return S.priceLabel(item); },
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
                return this.urls.base + 'beta/' + listing.id;
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
})();
