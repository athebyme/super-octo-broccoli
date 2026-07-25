/* Качество карточек · beta — очередь работ, а не таблица оценок.
   Порядок задаёт потенциал исправления: спрос есть, а покупки нет. */
(function () {
    'use strict';

    if (typeof window.shVue === 'undefined') return;
    var boot = window.shVue.bootstrapData('cq-bootstrap');

    /* Причины сортируем по тому, насколько быстро продавец может их закрыть */
    var REASON_META = {
        few_photos: { label: 'Мало фото', tone: 'warn', order: 1 },
        weak_chars: { label: 'Пустые характеристики', tone: 'warn', order: 2 },
        weak_description: { label: 'Слабое описание', tone: 'warn', order: 3 },
        weak_title: { label: 'Слабое название', tone: 'warn', order: 4 },
        low_cart_conv: { label: 'Не кладут в корзину', tone: 'danger', order: 5 },
        low_buyout: { label: 'Низкий выкуп', tone: 'danger', order: 6 },
        low_rating: { label: 'Низкий рейтинг', tone: 'danger', order: 7 },
        no_views: { label: 'Не видят покупатели', tone: 'info', order: 8 },
        no_sales_signal: { label: 'Нет данных о продажах', tone: 'muted', order: 9 },
    };

    /* Все размерности из WEIGHTS скорера — иначе ключ протечёт в интерфейс */
    var DIM_LABELS = {
        characteristics: 'Характеристики',
        photos: 'Фотографии',
        description: 'Описание',
        title: 'Название',
        brand: 'Бренд',
        barcodes: 'Штрихкоды',
        price: 'Цена',
        category: 'Категория WB',
        dimensions: 'Габариты',
    };

    window.shVue.mount('#card-quality-app', {
        data: function () {
            return {
                wbConnected: !!boot.wbConnected,
                urls: boot.urls || {},

                items: [],
                summary: {},
                page: 1,
                pages: 1,
                loading: true,
                loadingMore: false,
                refreshing: false,
                error: '',

                searchInput: '',
                filters: { search: '', reason: '', bucket: '', sort: 'impact' },
                view: localStorage.getItem('sh-cq-view') === 'table' ? 'table' : 'cards',
                selected: [],
                imgFailed: {},

                drawer: {
                    open: false, item: null, loading: false, loaded: false,
                    dims: [], breakdown: [], photos: [], recommendations: [],
                    cartConv: null, buyout: null, trend: [],
                },

                columns: [
                    { key: 'title', title: 'Товар' },
                    { key: 'quality_score', title: 'Оценка', align: 'right', sortable: false },
                    { key: 'attention_reasons', title: 'Что не так' },
                    { key: 'views_30d', title: 'Смотрят', format: 'int', align: 'right' },
                    { key: 'orders_30d', title: 'Заказы', format: 'int', align: 'right' },
                    { key: 'price', title: 'Цена', format: 'money', align: 'right' },
                    { key: 'quantity', title: 'Остаток', format: 'int', align: 'right' },
                ],
            };
        },

        computed: {
            chips: function () {
                var counts = (this.summary && this.summary.reason_counts) || {};
                var chips = [{ key: 'all', value: '', label: 'Все', count: this.summary.total }];
                Object.keys(REASON_META).forEach(function (code) {
                    var count = counts[code];
                    if (!count) return;
                    chips.push({
                        key: code,
                        value: code,
                        label: REASON_META[code].label,
                        count: count,
                        alarm: REASON_META[code].tone === 'danger',
                    });
                });
                return chips;
            },
            counts: function () {
                var losing = 0, invisible = 0, healthy = 0;
                this.items.forEach(function (item) {
                    var views = item.views_30d;
                    var orders = item.orders_30d;
                    if (views !== null && views >= 30 && !orders) losing++;
                    else if (views !== null && views < 30) invisible++;
                    if (item.quality_score !== null && item.quality_score >= 70 && orders) healthy++;
                });
                return { losing: losing, invisible: invisible, healthy: healthy };
            },
            scoreDelta: function () {
                // История приходит по возрастанию даты: сравниваем крайние точки
                var trend = this.drawer.trend || [];
                if (trend.length < 2) return null;
                var first = trend[0].quality_score;
                var last = trend[trend.length - 1].quality_score;
                if (typeof first !== 'number' || typeof last !== 'number') return null;
                var delta = last - first;
                return delta === 0 ? null : delta;
            },
            hasFilters: function () {
                return !!(this.filters.search || this.filters.reason || this.filters.bucket);
            },
            bulkImproveHref: function () {
                return this.urls.bulkImprove + '?ids=' + this.selected.join(',');
            },
            standardPhotosHref: function () {
                return this.urls.standardPhotos + '?ids=' + this.selected.join(',');
            },
        },

        watch: {
            searchInput: function (value) {
                var self = this;
                clearTimeout(this._searchTimer);
                this._searchTimer = setTimeout(function () {
                    var next = (value || '').trim().slice(0, 100);
                    if (next !== self.filters.search) self.filters.search = next;
                }, 300);
            },
            filters: { deep: true, handler: function () { this.load(); } },
        },

        mounted: function () {
            if (this.wbConnected) this.load();
        },

        methods: {
            load: function () {
                var self = this;
                if (this._abort) this._abort.abort();
                var abort = new AbortController();
                this._abort = abort;
                this.loading = true;
                this.error = '';
                this.$api.get(this.urls.list, {
                    search: this.filters.search,
                    reason: this.filters.reason,
                    bucket: this.filters.bucket,
                    sort: this.filters.sort,
                    page: 1,
                    per_page: 50,
                }, { signal: abort.signal }).then(function (data) {
                    if (abort.signal.aborted) return;
                    self.items = data.items || [];
                    self.summary = data.summary || {};
                    self.page = data.page || 1;
                    self.pages = data.pages || 1;
                    self.selected = [];
                }).catch(function (err) {
                    if (err && err.name === 'AbortError') return;
                    self.error = err.message || 'Не удалось загрузить список';
                }).finally(function () {
                    if (!abort.signal.aborted) self.loading = false;
                });
            },
            loadMore: function () {
                var self = this;
                this.loadingMore = true;
                this.$api.get(this.urls.list, {
                    search: this.filters.search,
                    reason: this.filters.reason,
                    bucket: this.filters.bucket,
                    sort: this.filters.sort,
                    page: this.page + 1,
                    per_page: 50,
                }).then(function (data) {
                    var seen = {};
                    self.items.forEach(function (item) { seen[item.product_id] = true; });
                    (data.items || []).forEach(function (item) {
                        if (!seen[item.product_id]) self.items.push(item);
                    });
                    self.page = data.page || self.page + 1;
                    self.pages = data.pages || self.pages;
                }).catch(function (err) {
                    self.error = err.message || 'Не удалось загрузить ещё';
                }).finally(function () {
                    self.loadingMore = false;
                });
            },
            refreshRatings: function () {
                var self = this;
                this.refreshing = true;
                this.$api.post(this.urls.refresh, {}).then(function () {
                    // Пересчёт идёт фоном: даём ему фору и перечитываем список
                    setTimeout(function () {
                        self.refreshing = false;
                        self.load();
                    }, 4000);
                }).catch(function (err) {
                    self.refreshing = false;
                    self.error = err.message || 'Не удалось запустить обновление';
                });
            },
            setReason: function (value) {
                this.filters.reason = this.filters.reason === value ? '' : value;
            },
            setView: function (value) {
                this.view = value;
                localStorage.setItem('sh-cq-view', value);
            },
            resetFilters: function () {
                this.searchInput = '';
                this.filters.search = '';
                this.filters.reason = '';
                this.filters.bucket = '';
            },

            /* ---------- деталь ---------- */
            openDetail: function (item) {
                var self = this;
                this.drawer.open = true;
                this.drawer.item = item;
                this.drawer.dims = [];
                this.drawer.breakdown = [];
                this.drawer.photos = [];
                this.drawer.recommendations = [];
                this.drawer.trend = [];
                this.drawer.cartConv = null;
                this.drawer.buyout = null;
                this.drawer.loaded = false;
                this.drawer.loading = true;
                this.$api.get(this.urls.detailBase + item.product_id).then(function (data) {
                    if (!self.drawer.item || self.drawer.item.product_id !== item.product_id) return;
                    var detail = data.data || data.detail || data;
                    var dims = (detail && detail.dimensions) || {};

                    // Проблемные размерности — с конкретной подсказкой
                    self.drawer.dims = Object.keys(dims).filter(function (key) {
                        var dim = dims[key] || {};
                        return dim.status === 'error' || dim.status === 'warning';
                    }).map(function (key) {
                        var dim = dims[key] || {};
                        return {
                            key: key,
                            label: DIM_LABELS[key] || key,
                            hint: dim.hint || dim.message || '',
                        };
                    });

                    // Полная разбивка оценки: видно, что именно тянет вниз
                    self.drawer.breakdown = Object.keys(dims).map(function (key) {
                        var dim = dims[key] || {};
                        var score = typeof dim.score === 'number' ? Math.round(dim.score) : 0;
                        return {
                            key: key,
                            label: DIM_LABELS[key] || key,
                            score: score,
                            weight: dim.weight || 0,
                            tone: score >= 70 ? 'ok' : (score >= 50 ? 'warn' : 'danger'),
                        };
                    }).sort(function (a, b) { return a.score - b.score; });

                    self.drawer.photos = (detail.photos || []).filter(function (url) {
                        return typeof url === 'string' && url;
                    });
                    self.drawer.recommendations = (detail.recommendations || []).slice(0, 6);
                    self.drawer.trend = detail.trend || [];
                    self.drawer.cartConv = typeof detail.wb_cart_conv === 'number' ? detail.wb_cart_conv : null;
                    self.drawer.buyout = typeof detail.wb_buyout_rate === 'number' ? detail.wb_buyout_rate : null;
                    self.drawer.loaded = true;
                }).catch(function () {
                    self.drawer.dims = [];
                }).finally(function () {
                    self.drawer.loading = false;
                });
            },
            closeDetail: function () {
                this.drawer.open = false;
                this.drawer.item = null;
            },

            /* ---------- отображение ---------- */
            letterOf: function (item) {
                var title = ((item && item.title) || '').trim();
                return title ? title[0].toUpperCase() : '·';
            },
            reasonsOf: function (item) {
                return (item.attention_reasons || []).map(function (code) {
                    var meta = REASON_META[code];
                    return meta
                        ? { code: code, label: meta.label, tone: meta.tone, order: meta.order }
                        : { code: code, label: code, tone: 'muted', order: 99 };
                }).sort(function (a, b) { return a.order - b.order; });
            },
            visibleReasons: function (item, limit) {
                return this.reasonsOf(item).slice(0, limit || 3);
            },
            hiddenReasonCount: function (item) {
                return Math.max(0, this.reasonsOf(item).length - 3);
            },
            scoreTone: function (score) {
                if (score === null || score === undefined) return 'muted';
                if (score >= 70) return 'ok';
                if (score >= 50) return 'warn';
                return 'danger';
            },
            verdictOf: function (item) {
                // Вердикт на языке продавца: что это значит для продаж
                var views = item.views_30d;
                var orders = item.orders_30d;
                if (views !== null && views >= 30 && !orders) return 'Смотрят, но не покупают';
                if (views !== null && views < 30) return 'Покупатели не находят';
                if (!item.quantity) return 'Нет на складе';
                if (item.quality_score !== null && item.quality_score >= 70) return 'В порядке';
                return 'Можно улучшить';
            },
        },
    });
})();
