/* Карточка товара — Vue 3 поверх seller-scoped detail и link API. */
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
    function acceptCsrf(data) {
        if (typeof data.csrf === 'string' && data.csrf.length >= 20 && data.csrf.length <= 512) CSRF = data.csrf;
    }

    var S = window.mcatShared;
    var linkController = null;
    var linkTimer = null;
    var linkReturnFocus = null;

    var candidatePhoto = {
        directives: {'image-deadline': S.imageDeadline},
        props: ['src', 'candidateId'],
        data: function () { return {attempt:0, manualCycle:0, state:'idle', timer:null}; },
        computed: {
            exactSource: function () {
                var match = typeof this.src === 'string' && this.src.match(/^\/api\/photos\/imported-product\/([1-9]\d*)\/(0|[1-9]\d*)\?deferred=1$/);
                return !!match && String(this.candidateId) === match[1];
            },
            url: function () {
                return this.src + (this.attempt ? '&preview_attempt=' + this.attempt : '') +
                    (this.manualCycle ? '&manual_retry=' + this.manualCycle : '');
            }
        },
        methods: {
            stopTimer: function () { clearTimeout(this.timer); this.timer = null; },
            reset: function () {
                this.stopTimer(); this.attempt = 0; this.manualCycle = 0;
                this.state = !this.exactSource ? 'missing' : document.hidden ? 'paused' : 'loading';
            },
            onVisibility: function () {
                if (document.hidden && (this.state === 'loading' || this.state === 'pending')) {
                    this.stopTimer(); this.state = 'paused';
                }
            },
            onLoad: function (event) {
                if (this.state === 'loading' && event.target === this.$refs.photo &&
                    event.target.currentSrc === new URL(this.url, location.href).href) {
                    this.stopTimer(); this.state = 'ready';
                }
            },
            onError: function (event) {
                if (this.state !== 'loading' || event.target !== this.$refs.photo ||
                    (event.target.getAttribute('src') && event.target.getAttribute('src') !== this.url)) return;
                this.stopTimer();
                if (document.hidden) { this.state = 'paused'; return; }
                if (this.attempt >= 3) { this.state = 'failed'; return; }
                this.state = 'pending';
                this.timer = setTimeout(function () {
                    this.timer = null;
                    if (document.hidden) { this.state = 'paused'; return; }
                    this.attempt += 1; this.state = 'loading';
                }.bind(this), 2000 * (this.attempt + 1));
            },
            retry: function () {
                if (document.hidden || (this.state !== 'failed' && this.state !== 'paused')) return;
                this.stopTimer(); this.manualCycle += 1; this.attempt = 0; this.state = 'loading';
            }
        },
        watch: {src: function () { this.reset(); }, candidateId: function () { this.reset(); }},
        mounted: function () { this.reset(); document.addEventListener('visibilitychange', this.onVisibility); },
        beforeUnmount: function () { this.stopTimer(); document.removeEventListener('visibilitychange', this.onVisibility); },
        template: '<span class="mdet-link-thumb">' +
            '<img v-image-deadline v-if="exactSource && (state === \'loading\' || state === \'ready\')" :key="url" ref="photo" :src="url" :class="{\'mdet-link-photo-loading\':state === \'loading\'}" width="64" height="64" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" @load="onLoad" @error="onError">' +
            '<span v-if="state === \'loading\' || state === \'pending\'" class="mdet-link-photo-state" role="status">Загружаем фото…</span>' +
            '<span v-else-if="state === \'missing\'" class="mdet-link-photo-state">Фото не найдено</span>' +
            '<span v-else-if="state === \'idle\'" aria-hidden="true"></span>' +
            '<button v-else-if="state === \'paused\'" type="button" class="mdet-link-photo-retry" :aria-label="\'Продолжить загрузку фото внутренней карточки \' + candidateId" @click.stop="retry">Продолжить фото</button>' +
            '<span v-else-if="state === \'failed\'" class="mdet-link-photo-failed" role="status"><button type="button" class="mdet-link-photo-retry" :aria-label="\'Повторить загрузку фото внутренней карточки \' + candidateId" @click.stop="retry"><span>Фото недоступно</span><span>Повторить</span></button></span>' +
            '</span>'
    };

    Vue.createApp({
        directives: {'image-deadline': S.imageDeadline},
        components: {'ozon-prices': S.ozonPrices, 'candidate-photo': candidatePhoto},
        data: function () {
            return {
                listingId: bootstrap.listingId,
                members: bootstrap.members || [],
                urls: bootstrap.urls || {},
                returnUrl: bootstrap.returnUrl || '/marketplaces/listings/',
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
                actionError: false,
                linkNeedsRead: false,
                linkReview: {open:false, mode:'', query:'', candidates:[], selected:null, checked:false, loading:false, saving:false, error:'', viewedVersion:null, pending:null, needsChoice:false, unknown:false},
                candidateImageErrors: {}
            };
        },

        computed: {
            currentMember: function () {
                var id = this.listingId;
                return this.members.find(function (m) { return m.id === id; }) || null;
            },
            overviewUrl: function () { return this.overviewUrlFor(this.listingId); },
            managementUrl: function () {
                return this.urls.managementBase + this.listingId + '?return_to=' + encodeURIComponent(this.returnUrl);
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
                window.history.replaceState({ listingId: this.listingId, returnUrl: this.returnUrl }, '');
            }
            this.load();
        },
        beforeUnmount: function () {
            window.removeEventListener('popstate', this._onPop);
            clearTimeout(linkTimer); if (linkController) linkController.abort();
        },

        methods: {
            load: function () {
                var self = this;
                var token = ++this.requestId;
                var listingId = this.listingId;
                this.loading = true;
                this.error = null;
                // Единый bootstrap: карточка + каналы + имена атрибутов + связь
                fetch(this.urls.base + 'view/' + listingId, {
                    headers: { Accept: 'application/json' }
                }).then(S.readJson).then(function (data) {
                    // Пользователь мог переключить канал, пока ответ был в пути
                    if (token !== self.requestId || !data.listing || data.listing.id !== listingId) return;
                    acceptCsrf(data);
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
                this.closeLinkReview();
                this.listingId = member.id;
                this.actionMessage = null;
                if (window.history && window.history.pushState) {
                    window.history.pushState(
                        { listingId: member.id, returnUrl: this.returnUrl },
                        '',
                        this.overviewUrlFor(member.id)
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
                if (typeof state.returnUrl === 'string') this.returnUrl = state.returnUrl;
                if (target && target !== this.listingId) {
                    this.closeLinkReview();
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
                        self.actionMessage = 'Связь с внутренним товаром установлена.';
                    } else if (status === 'ambiguous') {
                        self.actionMessage = 'Найдено несколько точных совпадений — выберите внутреннюю карточку вручную.';
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
            async linkRequest(url, options) {
                var controller = new AbortController();
                var timedOut = false;
                if (options && options.signal) options.signal.addEventListener('abort', function () { controller.abort(); }, {once:true});
                var timeout = setTimeout(function () { timedOut = true; controller.abort(); }, options && options.method === 'POST' ? 15000 : 10000);
                try {
                    var response = await fetch(url, {
                        ...options, signal:controller.signal, credentials:'same-origin',
                        headers:{Accept:'application/json', ...((options && options.method === 'POST') ? {'Content-Type':'application/json', 'X-CSRFToken':CSRF} : {})}
                    });
                    if (response.status === 401 || response.redirected) throw Object.assign(Error('Сессия истекла. Войдите заново, затем прочитайте текущее состояние.'), {status:401});
                    if (response.status === 403) throw Object.assign(Error('Доступ к действию закрыт. Проверьте вход и права, затем прочитайте текущее состояние.'), {status:403});
                    if (!(response.headers.get('content-type') || '').includes('application/json')) throw Error('Сервер вернул неожиданный ответ. Состояние нужно перечитать.');
                    var data = await response.json();
                    if (!response.ok || data.success === false) throw Object.assign(Error(data.error || 'Действие не выполнено.'), {status:response.status});
                    return data;
                } catch (error) {
                    if (timedOut) throw Error(options && options.method === 'POST'
                        ? 'Сервер не ответил вовремя. Результат записи неизвестен.'
                        : 'Сервер не ответил вовремя. Не удалось прочитать текущее состояние.');
                    throw error;
                } finally { clearTimeout(timeout); }
            },
            openLinkReview: function (mode) {
                if (!this.listing || this.loading || this.acting || this.listing.marketplace_code !== 'ozon') return;
                var actions = (this.productLink || {}).actions || {};
                if (mode === 'link' && !actions.can_link) return;
                if (mode === 'unlink' && !actions.can_unlink) return;
                linkReturnFocus = document.activeElement;
                this.linkReview = {open:true, mode:mode, query:'', candidates:[], selected:null, checked:false, loading:false, saving:false, error:'', viewedVersion:this.listing.link_version, pending:null, needsChoice:this.linkNeedsRead, unknown:this.linkNeedsRead};
                this.$nextTick(function () {
                    this.$refs.linkDialog.showModal();
                    (mode === 'link' ? this.$refs.linkSearch : this.$refs.linkTitle).focus();
                });
                if (mode === 'link' && !this.linkNeedsRead) this.loadLinkCandidates();
            },
            closeLinkReview: function () {
                if (this.linkReview.saving) return;
                clearTimeout(linkTimer); if (linkController) linkController.abort();
                if (this.$refs.linkDialog && this.$refs.linkDialog.open) this.$refs.linkDialog.close();
                this.linkReview.open = false;
                if (linkReturnFocus && linkReturnFocus.isConnected) linkReturnFocus.focus();
            },
            queueLinkSearch: function () {
                clearTimeout(linkTimer);
                this.linkReview.selected = null; this.linkReview.checked = false;
                linkTimer = setTimeout(this.loadLinkCandidates.bind(this), 250);
            },
            selectLinkCandidate: function (candidate, event) {
                if (event.target.closest('button')) return;
                this.linkReview.selected = candidate; this.linkReview.checked = false;
            },
            loadLinkCandidates: async function () {
                var review = this.linkReview;
                if (!review.open || review.mode !== 'link' || review.saving || review.needsChoice || review.query.length > 200) return;
                if (linkController) linkController.abort();
                linkController = new AbortController(); var current = linkController;
                review.loading = true; review.error = '';
                try {
                    var data = await this.linkRequest(this.urls.base + 'view/' + this.listingId + '/link-candidates?q=' + encodeURIComponent(review.query), {signal:current.signal});
                    if (current !== linkController || !review.open) return;
                    if (data.listing_id !== this.listingId || data.link_version !== review.viewedVersion) {
                        review.needsChoice = true; review.error = 'Связь изменилась. Прочитайте сохранённое состояние перед новым выбором.'; return;
                    }
                    review.candidates = (data.candidates || []).slice(0, 20);
                } catch (error) {
                    if (current === linkController && error.name !== 'AbortError') review.error = error.message || 'Поиск не удался.';
                } finally { if (current === linkController) review.loading = false; }
            },
            readLinkCurrent: async function () {
                var review = this.linkReview;
                if (!review.open || review.loading || review.saving) return;
                review.loading = true; review.error = '';
                try {
                    var data = await this.linkRequest(this.urls.base + 'view/' + this.listingId);
                    if (!review.open || !data.listing || data.listing.id !== this.listingId) return;
                    acceptCsrf(data);
                    review.pending = data; review.needsChoice = true; review.checked = false;
                } catch (error) { review.error = error.message || 'Не удалось прочитать сохранённую связь.'; }
                finally { review.loading = false; }
            },
            adoptLinkCurrent: function () {
                var review = this.linkReview;
                if (!review.pending || review.loading || review.saving) return;
                this.listing = review.pending.listing;
                this.productLink = review.pending.product_link;
                this.members = review.pending.members || this.members;
                review.viewedVersion = this.listing.link_version;
                review.pending = null; review.needsChoice = false; review.unknown = false; review.checked = false; review.selected = null;
                this.linkNeedsRead = false;
                if (review.mode === 'link' && this.productLink?.actions?.can_link) this.loadLinkCandidates();
            },
            submitReviewedLink: async function () {
                var review = this.linkReview;
                var actions = (this.productLink || {}).actions || {};
                if (!review.open || review.loading || review.saving || review.needsChoice || !review.checked || this.loading || this.acting || review.viewedVersion !== this.listing.link_version) return;
                if (review.mode === 'link' && (!actions.can_link || !review.selected || !review.candidates.some(function (item) { return item.id === review.selected.id; }))) return;
                if (review.mode === 'unlink' && !actions.can_unlink) return;
                review.saving = true; review.error = ''; review.checked = false;
                var body = {expected_link_version:review.viewedVersion};
                if (review.mode === 'link') body.imported_product_id = review.selected.id;
                try {
                    await this.linkRequest(this.urls.base + this.listingId + (review.mode === 'link' ? '/link' : '/unlink'), {method:'POST', body:JSON.stringify(body)});
                    this.actionMessage = review.mode === 'link' ? 'Связь сохранена.' : 'Связь удалена.';
                    this.actionError = false; this.linkNeedsRead = false;
                    review.saving = false; this.closeLinkReview(); this.load();
                } catch (error) {
                    review.error = error.message || 'Действие не выполнено.';
                    if (!error.status || error.status >= 500 || error.status === 409 || error.status === 401 || error.status === 403) {
                        review.needsChoice = true; review.unknown = !error.status || error.status >= 500;
                        this.linkNeedsRead = true;
                    }
                } finally { review.saving = false; }
            },

            /* ---------- отображение (общий слой mcatShared) ---------- */
            memberLabel: function (member) {
                if (member.marketplace_code === 'wb') return 'Wildberries';
                return member.account_label ? 'Ozon · ' + member.account_label : 'Ozon';
            },
            overviewUrlFor: function (listingId) {
                return this.urls.overviewBase + listingId + '?return_to=' + encodeURIComponent(this.returnUrl);
            },
            channelFull: function (listing) {
                if (listing.marketplace_code === 'wb') return 'Wildberries';
                return 'Ozon';
            },
            letterOf: function (listing) { return S.letterOf(listing); },
            statusMeta: function (listing) { return S.statusMeta(listing); },
            linkLabel: function (listing) {
                if (!listing) return '';
                if (listing.link_status === 'linked') return 'Связан с внутренним товаром';
                if (listing.link_status === 'ambiguous') return 'Несколько совпадений';
                return 'Без связи с внутренним товаром';
            },
            linkSourceLabel: function (source) { return S.linkSourceLabel(source); },
            fmtMoney: function (value) { return S.fmtMoney(value, this.currency); },
            priceOf: function (listing) { return S.priceLabel(listing); },
            relTime: function (iso) { return S.relTime(iso); }
        }
    }).mount('#marketplace-detail-app');
})();
