/* Карточка товара (beta) — Vue 3 поверх существующего detail JSON API.
   Read-only + существующие link-действия (reconcile/unlink) с confirm. */
(function () {
    'use strict';

    var bootstrapEl = document.getElementById('mdet-bootstrap');
    var appEl = document.getElementById('marketplace-detail-app');
    if (!bootstrapEl || !appEl || typeof Vue === 'undefined') return;

    var bootstrap;
    try {
        bootstrap = JSON.parse(bootstrapEl.textContent);
    } catch (err) {
        return;
    }

    var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

    var S = window.mcatShared;

    Vue.createApp({
        data: function () {
            return {
                listingId: bootstrap.listingId,
                members: bootstrap.members || [],
                urls: bootstrap.urls || {},
                ozonEnabled: !!bootstrap.ozonEnabled,

                listing: null,
                warehouseStocks: [],
                attributeNames: {},
                productLink: null,
                serverGallery: [],
                imgFailed: {},
                requestId: 0,
                loading: true,
                error: null,
                heroOverride: null,
                descOpen: false,
                charsOpen: false,
                acting: false,
                actionMessage: null,
                actionError: false
            };
        },

        computed: {
            currentMember: function () {
                var id = this.listingId;
                return this.members.find(function (m) { return m.id === id; }) || null;
            },
            wbPublicUrl: function () {
                var member = this.currentMember;
                if (member && member.wb_nm_id) {
                    return 'https://www.wildberries.ru/catalog/' + member.wb_nm_id + '/detail.aspx';
                }
                return null;
            },
            gallery: function () {
                var self = this;
                var urls = [];
                var push = function (value) {
                    if (typeof value === 'string' && /^https?:\/\//.test(value)
                        && urls.indexOf(value) === -1 && !self.imgFailed[value]) {
                        urls.push(value);
                    }
                };
                (this.serverGallery || []).forEach(push);
                var media = (this.listing && this.listing.media) || {};
                push(media.primary_image);
                (Array.isArray(media.images) ? media.images : []).forEach(push);
                if (this.listing) {
                    push(this.listing.primary_image);
                    push(this.listing.hover_image);
                }
                return urls.slice(0, 10);
            },
            heroSrc: function () {
                if (this.heroOverride && !this.imgFailed[this.heroOverride]) {
                    return this.heroOverride;
                }
                return this.gallery[0] || null;
            },
            currency: function () {
                return S.priceFacts(this.listing).currency;
            },
            oldPrice: function () { return S.oldPriceLabel(this.listing); },
            minPrice: function () { return S.minPriceLabel(this.listing); },
            discount: function () { return S.discountPercent(this.listing); },
            stockN: function () { return S.stockNumber(this.listing); },
            stockLabel: function () {
                if (this.stockN === null) return 'остаток неизвестен';
                if (this.stockN === 0) return 'нет на остатках';
                return 'на остатках ' + S.fmtInt(this.stockN) + ' шт';
            },
            stockRows: function () { return S.stockRows(this.listing); },
            moderationErrors: function () {
                var raw = (this.listing && this.listing.moderation_errors) || [];
                return raw.slice(0, 20).map(function (entry) {
                    if (typeof entry === 'string') return entry;
                    if (entry && typeof entry === 'object') {
                        return entry.description || entry.message || entry.error ||
                            entry.code || JSON.stringify(entry).slice(0, 200);
                    }
                    return String(entry);
                });
            },
            barcodes: function () {
                var raw = (this.listing && this.listing.barcodes) || [];
                return raw.filter(function (v) { return typeof v === 'string' && v; }).slice(0, 10);
            },
            canonical: function () {
                var canonical = this.productLink && this.productLink.canonical_product;
                return canonical && this.listing
                    && canonical.id === this.listing.imported_product_id
                    ? canonical : null;
            },
            otherMembers: function () {
                var id = this.listingId;
                return this.members.filter(function (m) { return m.id !== id; });
            },
            hasContentColumn: function () {
                return !!(this.listing && (
                    this.listing.description
                    || this.barcodes.length
                    || this.listing.external_category_id
                    || this.characteristics.length
                    || this.complexCharacteristics.length
                    || this.dimensionsText
                ));
            },
            providerMeta: function () { return S.providerStatusMeta(this.listing); },
            providerBadge: function () {
                var meta = this.providerMeta;
                return meta && (meta.tone === 'danger' || meta.tone === 'warn') ? meta : null;
            },
            visibilityMeta: function () { return S.visibilityMeta(this.listing); },
            visibilityBadge: function () {
                var meta = this.visibilityMeta;
                return meta && meta.tone !== 'ok' ? meta : null;
            },
            characteristics: function () {
                var raw = (this.listing && this.listing.attributes) || [];
                var names = this.attributeNames || {};
                var rows = [];
                (Array.isArray(raw) ? raw : []).forEach(function (entry) {
                    if (!entry || typeof entry !== 'object') return;
                    var id = entry.id;
                    var values = (Array.isArray(entry.values) ? entry.values : [])
                        .map(function (v) {
                            return v && typeof v === 'object' ? v.value : v;
                        })
                        .filter(function (v) { return typeof v === 'string' && v.trim(); });
                    if (!values.length) return;
                    var name = names[id];
                    rows.push({
                        id: String(id),
                        name: name || ('Атрибут ' + id),
                        named: !!name,
                        value: values.join(', ').slice(0, 500)
                    });
                });
                rows.sort(function (a, b) {
                    if (a.named !== b.named) return a.named ? -1 : 1;
                    if (!a.named) return Number(a.id) - Number(b.id);
                    return a.name.localeCompare(b.name, 'ru');
                });
                return rows.slice(0, 100);
            },
            complexCharacteristics: function () {
                var names = this.attributeNames || {};
                var raw = (this.listing && this.listing.complex_attributes) || [];
                var groups = [];
                (Array.isArray(raw) ? raw : []).slice(0, 20).forEach(function (container, index) {
                    var attrs = (container && container.attributes) || [];
                    var rows = [];
                    (Array.isArray(attrs) ? attrs : []).forEach(function (entry) {
                        if (!entry || typeof entry !== 'object') return;
                        var values = (Array.isArray(entry.values) ? entry.values : [])
                            .map(function (v) { return v && typeof v === 'object' ? v.value : v; })
                            .filter(function (v) { return typeof v === 'string' && v.trim(); });
                        if (!values.length) return;
                        var name = names[entry.id];
                        rows.push({
                            id: String(entry.id),
                            name: name || ('Атрибут ' + entry.id),
                            named: !!name,
                            value: values.join(', ').slice(0, 500)
                        });
                    });
                    if (rows.length) groups.push({ key: 'c' + index, rows: rows });
                });
                return groups;
            },
            visibleCharacteristics: function () {
                return this.charsOpen ? this.characteristics : this.characteristics.slice(0, 10);
            },
            dimensionsText: function () { return S.dimensionsLabel(this.listing); }
        },

        mounted: function () {
            this._onPop = this.onPopState.bind(this);
            window.addEventListener('popstate', this._onPop);
            if (window.history && window.history.replaceState) {
                window.history.replaceState({ listingId: this.listingId }, '');
            }
            this.load();
        },
        beforeUnmount: function () {
            window.removeEventListener('popstate', this._onPop);
        },

        methods: {
            load: function () {
                var self = this;
                var token = ++this.requestId;
                var listingId = this.listingId;
                this.loading = true;
                this.error = null;
                // Единый bootstrap: карточка + каналы + имена атрибутов + связь
                fetch(this.urls.base + 'beta/' + listingId, {
                    headers: { Accept: 'application/json' }
                }).then(S.readJson).then(function (data) {
                    // Пользователь мог переключить канал, пока ответ был в пути
                    if (token !== self.requestId) return;
                    self.listing = data.listing;
                    self.serverGallery = data.gallery || [];
                    self.warehouseStocks = (data.warehouse_stocks || []).slice(0, 20);
                    self.attributeNames = data.attribute_names || {};
                    self.productLink = data.product_link || null;
                    if (Array.isArray(data.members) && data.members.length) {
                        self.members = data.members;
                    }
                    self.heroOverride = null;
                    self.imgFailed = {};
                    self.descOpen = false;
                    self.charsOpen = false;
                }).catch(function (err) {
                    if (token !== self.requestId) return;
                    self.error = err.message || 'Не удалось загрузить карточку';
                }).finally(function () {
                    if (token === self.requestId) self.loading = false;
                });
            },
            imageFailed: function (url) {
                if (url) this.imgFailed[url] = true;
            },
            switchMember: function (member) {
                if (member.id === this.listingId) return;
                this.listingId = member.id;
                this.actionMessage = null;
                if (window.history && window.history.pushState) {
                    window.history.pushState(
                        { listingId: member.id },
                        '',
                        this.urls.base + 'beta/' + member.id
                    );
                }
                this.load();
            },
            onPopState: function (event) {
                var state = event.state || {};
                var fromUrl = parseInt(
                    (window.location.pathname.split('/').pop() || ''),
                    10
                );
                var target = state.listingId || (isFinite(fromUrl) ? fromUrl : null);
                if (target && target !== this.listingId) {
                    this.listingId = target;
                    this.actionMessage = null;
                    this.load();
                }
            },
            postAction: function (path, body, confirmText) {
                var self = this;
                if (this.acting) return;
                if (confirmText && !window.confirm(confirmText)) return;
                this.acting = true;
                this.actionMessage = null;
                this.actionError = false;
                fetch(this.urls.base + this.listingId + path, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        Accept: 'application/json',
                        'X-CSRFToken': CSRF
                    },
                    body: JSON.stringify(body || {})
                }).then(S.readJson).then(function (data) {
                    if (data.listing) self.listing = data.listing;
                    var status = (data.listing || {}).link_status;
                    if (data.busy) {
                        self.actionMessage = 'Сейчас идёт фоновая сверка каталога — повторите через минуту.';
                    } else if (status === 'linked') {
                        self.actionMessage = 'Связь с общей карточкой установлена.';
                    } else if (status === 'ambiguous') {
                        self.actionMessage = 'Найдено несколько точных совпадений — выберите карточку вручную в классической версии.';
                    } else {
                        self.actionMessage = 'Точного совпадения не найдено. Связь можно выбрать вручную.';
                    }
                    // Связь изменилась — каналы товара пересобираем заново
                    self.load();
                }).catch(function (err) {
                    self.actionError = true;
                    self.actionMessage = err.message || 'Действие не выполнено';
                    // Конфликт оптимистичной версии: подтягиваем актуальное состояние
                    if (/изменилась|conflict|версия/i.test(err.message || '')) {
                        self.load();
                    }
                }).finally(function () {
                    self.acting = false;
                });
            },
            reconcileLink: function () {
                this.postAction('/reconcile-link', {});
            },
            unlink: function () {
                var version = this.listing && this.listing.link_version;
                this.postAction(
                    '/unlink',
                    { expected_link_version: version },
                    'Убрать связь этого листинга с общей внутренней карточкой? Контент и фото карточки не изменятся.'
                );
            },

            /* ---------- отображение (общий слой mcatShared) ---------- */
            memberLabel: function (member) {
                if (member.marketplace_code === 'wb') return 'Wildberries';
                return member.account_label ? 'Ozon · ' + member.account_label : 'Ozon';
            },
            channelFull: function (listing) {
                if (listing.marketplace_code === 'wb') return 'Wildberries';
                return listing.account_label ? 'Ozon · ' + listing.account_label : 'Ozon';
            },
            letterOf: function (listing) { return S.letterOf(listing); },
            statusMeta: function (listing) { return S.statusMeta(listing); },
            linkLabel: function (listing) {
                if (!listing) return '';
                if (listing.link_status === 'linked') return 'Связан с общей карточкой';
                if (listing.link_status === 'ambiguous') return 'Несколько совпадений';
                return 'Без связи с общей карточкой';
            },
            linkSourceLabel: function (source) { return S.linkSourceLabel(source); },
            fmtMoney: function (value) { return S.fmtMoney(value, this.currency); },
            priceOf: function (listing) { return S.priceLabel(listing); },
            relTime: function (iso) { return S.relTime(iso); }
        }
    }).mount('#marketplace-detail-app');
})();
