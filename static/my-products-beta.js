/* Мои товары · beta — экран массовых операций.
   Правило страницы: выбрал строки → видишь, что произойдёт → подтвердил →
   следишь за прогрессом → получаешь честный итог с возможностью доделать. */
(function () {
    'use strict';

    if (typeof window.shVue === 'undefined') return;
    var boot = window.shVue.bootstrapData('mp-bootstrap');
    var ozonPendingKey = 'ozon-source-prepare-v1';
    function readOzonPending() {
        try { return JSON.parse(sessionStorage.getItem(ozonPendingKey) || 'null'); }
        catch (_) { return null; }
    }
    function hasOzonPending() {
        try { return sessionStorage.getItem(ozonPendingKey) !== null; }
        catch (_) { return false; }
    }
    function validOzonPending(value, accounts) {
        return !!value && Number.isSafeInteger(value.account_id) &&
            accounts.some(function (row) { return row.id === value.account_id; }) &&
            typeof value.request_key === 'string' &&
            /^[A-Za-z0-9_-]{24,128}$/.test(value.request_key) &&
            Array.isArray(value.imported_product_ids) &&
            value.imported_product_ids.length > 0 && value.imported_product_ids.length <= 200 &&
            value.imported_product_ids.every(function (id) { return Number.isSafeInteger(id) && id > 0; }) &&
            new Set(value.imported_product_ids).size === value.imported_product_ids.length;
    }
    function saveOzonPending(value) {
        try { sessionStorage.setItem(ozonPendingKey, JSON.stringify(value)); return true; }
        catch (_) { return false; }
    }
    function clearOzonPending() {
        try { sessionStorage.removeItem(ozonPendingKey); } catch (_) {}
    }
    function ozonRequestKey() {
        if (window.crypto.randomUUID) return window.crypto.randomUUID().replace(/-/g, '');
        return Array.from(window.crypto.getRandomValues(new Uint8Array(16)), function (byte) {
            return byte.toString(16).padStart(2, '0');
        }).join('');
    }

    var MAX_BULK_SELECTION = 200;
    function exactWbProductIds(items) {
        if (!Array.isArray(items) || !items.length) return null;
        var ids = items.map(function (item) {
            return item && item.wb ? item.wb.product_id : null;
        });
        if (ids.some(function (id) {
            return !Number.isSafeInteger(id) || id <= 0;
        }) || new Set(ids).size !== ids.length) return null;
        return ids;
    }
    function sameIds(left, right) {
        if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
        if (new Set(left).size !== left.length || new Set(right).size !== right.length) return false;
        var rightSet = new Set(right);
        return left.every(function (id) { return rightSet.has(id); });
    }

    function labelProduct(item) {
        var title = item && typeof item.title === 'string' ? item.title.trim() : '';
        var vendorCode = item && typeof item.vendor_code === 'string' ? item.vendor_code.trim() : '';
        if (title && vendorCode && title !== vendorCode) return title + ' · ' + vendorCode;
        return title || vendorCode || 'Товар из выбранного списка';
    }

    function selectedProductLabels(items, ids) {
        var byId = new Map((items || []).map(function (item) { return [item.id, item]; }));
        return ids.map(function (id) {
            return { productId: id, label: labelProduct(byId.get(id)) };
        });
    }

    function nonnegativeCount(value) {
        return Number.isSafeInteger(value) && value >= 0;
    }

    function supplierRefreshOutcome(data, ids, labels, commonContentUrl) {
        if (!data || data.success !== true ||
            !['completed', 'completed_with_errors'].includes(data.status) ||
            !nonnegativeCount(data.requested) || data.requested !== ids.length ||
            !nonnegativeCount(data.updated) || !nonnegativeCount(data.skipped) ||
            !nonnegativeCount(data.errors) ||
            data.updated + data.skipped + data.errors !== data.requested ||
            (data.status === 'completed') !== (data.errors === 0) ||
            !Array.isArray(data.items) || data.items.length !== ids.length) return null;

        var submitted = new Set(ids);
        var rowsById = new Map();
        var itemCounts = { updated: 0, skipped: 0, error: 0 };
        for (var i = 0; i < data.items.length; i += 1) {
            var item = data.items[i];
            if (!item || !Number.isSafeInteger(item.product_id) ||
                !submitted.has(item.product_id) || rowsById.has(item.product_id)) return null;

            var row;
            if (item.status === 'updated' && item.reason === null) {
                row = {
                    status: 'updated',
                    statusLabel: 'Обновлено',
                    reasonLabel: 'Данные поставщика сохранены в Seller Hub.',
                    tone: 'ok',
                };
            } else if (item.status === 'skipped' && item.reason === 'no_supplier_link') {
                row = {
                    status: 'skipped',
                    statusLabel: 'Пропущено',
                    reasonLabel: 'Нет связи с каталогом поставщика. Данные не менялись.',
                    tone: 'muted',
                };
            } else if (item.status === 'error' && item.reason === 'update_failed') {
                row = {
                    status: 'error',
                    statusLabel: 'Не обновлено',
                    reasonLabel: 'Не удалось сохранить данные. Откройте карточку и проверьте её.',
                    tone: 'danger',
                    href: commonContentUrl
                        ? commonContentUrl + '?product_id=' + encodeURIComponent(item.product_id)
                        : '',
                };
            } else {
                return null;
            }
            itemCounts[row.status] += 1;
            rowsById.set(item.product_id, Object.assign({
                productId: item.product_id,
                label: labels.find(function (entry) { return entry.productId === item.product_id; })?.label
                    || 'Товар из выбранного списка',
            }, row));
        }

        if (rowsById.size !== ids.length || itemCounts.updated !== data.updated ||
            itemCounts.skipped !== data.skipped || itemCounts.error !== data.errors) return null;

        var summary = 'Обновление из поставщика. В запросе товаров: ' + data.requested + '.';

        var title = data.errors
            ? 'Обновление завершено с ошибками'
            : data.updated === 0 && data.skipped > 0
                ? 'Нет карточек для обновления'
                : 'Обновление из поставщика завершено';
        return {
            kind: data.errors ? 'partial' : 'complete',
            title: title,
            summary: summary,
            requested: data.requested,
            updated: data.updated,
            skipped: data.skipped,
            errors: data.errors,
            entries: ids.map(function (id) { return rowsById.get(id); }),
        };
    }

    function supplierRefreshNotice(kind, requested, message) {
        var rejected = kind === 'rejected';
        return {
            kind: kind,
            title: rejected ? 'Выбранные товары не обновлялись' : 'Итог обновления не подтверждён',
            summary: 'Обновление из поставщика. В запросе товаров: '
                + requested + '. ' + message,
            requested: requested,
            entries: [],
        };
    }

    var TABS = [
        { key: '', label: 'Все', countKey: 'all' },
        { key: 'pending', label: 'В работе', countKey: 'pending' },
        { key: 'validated', label: 'Готовы к WB', countKey: 'validated' },
        { key: 'imported', label: 'На WB', countKey: 'imported' },
        { key: 'imported_pending_sync', label: 'Ждут артикул WB', countKey: 'imported_pending_sync' },
        { key: 'failed', label: 'Ошибки', countKey: 'failed' },
        { key: 'updates', label: 'Обновления поставщика', countKey: 'supplier_updates' },
    ];

    window.shVue.mount('#my-products-app', {
        data: function () {
            var savedOzon = readOzonPending();
            return {
                wbConnected: !!boot.wbConnected,
                ozonEnabled: !!boot.ozonEnabled,
                ozonAccounts: boot.ozonAccounts || [],
                suppliers: boot.suppliers || [],
                urls: boot.urls || {},

                items: [],
                facets: {},
                pagination: {},
                loading: true,
                loadingMore: false,
                error: '',

                activeTab: '',
                searchInput: '',
                filters: {
                    search: '', supplier: '', has_photos: '', stock: '', sort: '',
                },
                selected: [],
                ozonIds: [],
                ozonAccountId: '',
                ozonConfirmed: false,
                ozonBusy: '',
                ozonError: '',
                ozonPending: validOzonPending(savedOzon, boot.ozonAccounts || []) ? savedOzon : null,
                ozonStorageProblem: hasOzonPending() && !validOzonPending(savedOzon, boot.ozonAccounts || []),
                ozonRetrySameKey: false,
                ozonSessionEnded: false,
                imgFailed: {},
                tabs: TABS,

                job: null,
                jobTitle: 'Выполняем',
                supplierRefreshResult: null,
                confirm: {
                    open: false, key: null, title: '', message: '',
                    details: [], label: 'Продолжить', danger: false, busy: false,
                    productIds: [], selectionIds: [], labels: [],
                },
            };
        },

        computed: {
            allSelected: function () {
                return this.items.length > 0 && this.selected.length === this.items.length;
            },
            someSelected: function () {
                return this.selected.length > 0 && !this.allSelected;
            },
            selectedItems: function () {
                var chosen = this.selected;
                return this.items.filter(function (item) {
                    return chosen.indexOf(item.id) !== -1;
                });
            },
            /* Основные операции показываются вместе; редкие — в «Ещё». */
            actionGroups: function () {
                var chosen = this.selectedItems;
                var createRows = chosen.filter(function (item) {
                    return !item.wb && ['pending', 'failed', 'validated'].indexOf(item.import_status) !== -1;
                });
                var wbIds = exactWbProductIds(chosen);
                var withUpdates = chosen.filter(function (i) { return i.has_supplier_update; }).length;
                var underLimit = chosen.length <= MAX_BULK_SELECTION;
                return [{
                    title: 'Основные действия',
                    items: [
                        {
                            key: 'push_wb',
                            label: 'Создать карточки WB',
                            hint: !this.wbConnected
                                ? 'подключите кабинет Wildberries'
                                : !underLimit
                                ? 'за один запуск можно выбрать не больше 200 товаров'
                                : createRows.length
                                    ? 'для ' + createRows.length + ' товаров, ещё не связанных с WB'
                                    : 'выберите товары, которые ещё не связаны с WB',
                            disabled: !this.wbConnected || !underLimit || !createRows.length,
                        },
                        this.ozonEnabled ? {
                            key: 'ozon',
                            label: 'Подготовить для Ozon',
                            hint: !underLimit
                                ? 'за один запуск можно выбрать не больше 200 товаров'
                                : !this.ozonAccounts.length
                                    ? 'сначала подключите магазин Ozon'
                                    : this.ozonBusy
                                        ? 'дождитесь завершения текущего действия'
                                    : this.ozonPending || this.ozonStorageProblem || this.ozonSessionEnded
                                        ? 'сначала проверьте сохранённый результат подготовки'
                                : 'локальные черновики в выбранном магазине; отправка подтверждается отдельно',
                            disabled: !underLimit || !this.ozonAccounts.length || !!this.ozonPending || this.ozonStorageProblem || !!this.ozonBusy || this.ozonSessionEnded,
                        } : null,
                        {
                            key: 'refresh_supplier',
                            label: 'Обновить из поставщика',
                            hint: !underLimit
                                ? 'за один запуск можно выбрать не больше 200 товаров'
                                : withUpdates
                                    ? withUpdates + ' с новыми данными; изменения останутся в Seller Hub'
                                    : 'обновит только ваши данные в Seller Hub, карточки площадок не меняются',
                            disabled: !underLimit,
                        },
                        {
                            key: 'common_content',
                            label: 'Изменить общий контент',
                            hint: chosen.length > 50
                                ? 'за один раз можно открыть не больше 50 товаров'
                                : 'изменит данные в Seller Hub; карточки площадок не обновятся',
                            disabled: !chosen.length || chosen.length > 50,
                        },
                        {
                            key: 'wb_prices',
                            label: 'Изменить цены WB',
                            hint: !underLimit
                                ? 'за один запуск можно выбрать не больше 200 товаров'
                                : !wbIds
                                    ? chosen.some(function (item) { return !item.wb; })
                                        ? 'выберите только товары с карточкой Wildberries'
                                        : 'у части выбора нет связанного ID карточки WB'
                                    : !this.wbConnected
                                        ? 'подключите кабинет Wildberries'
                                        : 'откроется проверка выбранного набора цен',
                            disabled: !underLimit || !wbIds || !this.wbConnected,
                        },
                    ].filter(Boolean),
                }];
            },
            secondaryActions: function () {
                var chosen = this.selectedItems;
                var published = chosen.filter(function (item) { return item.wb; }).length;
                var wbIds = exactWbProductIds(chosen);
                var underLimit = chosen.length <= MAX_BULK_SELECTION;
                return [
                    {
                        key: 'audit',
                        label: 'Сверить с Wildberries',
                        hint: !underLimit
                            ? 'за один запуск можно выбрать не больше 200 товаров'
                            : !this.wbConnected
                                ? 'подключите кабинет Wildberries'
                                : !wbIds
                                ? chosen.some(function (item) { return !item.wb; })
                                    ? 'выберите только опубликованные карточки WB'
                                    : 'у части выбора нет связанного ID карточки WB'
                                : 'только чтение: покажет расхождения фото и характеристик',
                        disabled: !this.wbConnected || !wbIds || !underLimit,
                    },
                    {
                        key: 'enrich',
                        label: 'Дополнить карточки на WB',
                        hint: wbIds && underLimit
                            ? 'выбрать фото и характеристики на экране проверки'
                            : 'нужны только опубликованные карточки WB; лимит 200',
                        disabled: !wbIds || !underLimit,
                    },
                    {
                        key: 'delete',
                        label: 'Удалить из «Моих товаров»',
                        hint: published
                            ? published + ' ' + this.$fmt.plural(published, 'карточка WB останется', 'карточки WB останутся', 'карточек WB останутся') + ' на площадке'
                            : 'товары снова станут доступны для импорта',
                        danger: true,
                        disabled: !underLimit,
                    },
                ];
            },
            emptyState: function () {
                if (this.filters.search || this.filters.supplier
                    || this.filters.has_photos || this.filters.stock) {
                    return {
                        title: 'По этим условиям товаров нет',
                        text: 'Попробуйте убрать часть фильтров.',
                        action: 'reset',
                    };
                }
                if (this.activeTab === 'updates') {
                    return {
                        title: 'Новых данных у поставщика нет',
                        text: 'Как только поставщик обновит карточки, они появятся здесь.',
                        action: 'none',
                    };
                }
                return {
                    title: 'Здесь пока пусто',
                    text: 'Добавьте товары из каталога поставщика — потом их можно будет отправить на маркетплейс.',
                    action: 'catalog',
                };
            },
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
            filters: { deep: true, handler: function () { this.load(); } },
        },

        mounted: function () {
            this.load();
            this.loadFacets();
        },
        beforeUnmount: function () {
            if (this._jobTimer) clearInterval(this._jobTimer);
        },

        methods: {
            queryParams: function (page) {
                var params = {
                    search: this.filters.search,
                    supplier: this.filters.supplier,
                    has_photos: this.filters.has_photos,
                    stock: this.filters.stock,
                    sort: this.filters.sort,
                    page: page || 1,
                    per_page: 50,
                };
                if (this.activeTab === 'updates') params.updates = '1';
                else if (this.activeTab) params.status = this.activeTab;
                return params;
            },
            load: function () {
                var self = this;
                if (this._abort) this._abort.abort();
                var abort = new AbortController();
                this._abort = abort;
                this.loading = true;
                this.error = '';
                this.$api.get(this.urls.feed, this.queryParams(1), { signal: abort.signal })
                    .then(function (data) {
                        if (abort.signal.aborted) return;
                        self.items = data.items || [];
                        self.pagination = data.pagination || {};
                        self.selected = [];
                    }).catch(function (err) {
                        if (err && err.name === 'AbortError') return;
                        self.error = err.message || 'Не удалось загрузить товары';
                    }).finally(function () {
                        if (!abort.signal.aborted) self.loading = false;
                    });
            },
            loadMore: function () {
                var self = this;
                this.loadingMore = true;
                this.$api.get(this.urls.feed, this.queryParams((this.pagination.page || 1) + 1))
                    .then(function (data) {
                        var seen = {};
                        self.items.forEach(function (item) { seen[item.id] = true; });
                        (data.items || []).forEach(function (item) {
                            if (!seen[item.id]) self.items.push(item);
                        });
                        self.pagination = data.pagination || self.pagination;
                    }).catch(function (err) {
                        self.error = err.message || 'Не удалось загрузить ещё';
                    }).finally(function () { self.loadingMore = false; });
            },
            loadFacets: function () {
                var self = this;
                this.$api.get(this.urls.facets).then(function (data) {
                    self.facets = data.statuses || {};
                }).catch(function () { /* счётчики некритичны */ });
            },
            setTab: function (key) {
                if (this.activeTab === key) return;
                this.activeTab = key;
                this.selected = [];
                this.load();
            },
            resetFilters: function () {
                this.searchInput = '';
                this.filters.search = '';
                this.filters.supplier = '';
                this.filters.has_photos = '';
                this.filters.stock = '';
            },
            toggleAll: function () {
                this.selected = this.allSelected
                    ? []
                    : this.items.map(function (item) { return item.id; });
            },

            /* ---------- массовые действия ---------- */
            startAction: function (action) {
                var chosen = this.selectedItems;
                var selectionIds = this.selected.slice();
                if (!chosen.length || selectionIds.length !== chosen.length ||
                    new Set(selectionIds).size !== selectionIds.length ||
                    chosen.some(function (item) { return selectionIds.indexOf(item.id) === -1; })) {
                    this.error = 'Выбор изменился. Обновите список и отметьте товары заново.';
                    return;
                }
                var published = chosen.filter(function (i) { return i.wb; }).length;
                var createIds = chosen.filter(function (item) {
                    return !item.wb && ['pending', 'failed', 'validated'].indexOf(item.import_status) !== -1;
                }).map(function (item) { return item.id; });
                var plans = {
                    push_wb: {
                        title: 'Создать карточки WB',
                        message: 'Попробуем создать карточки для ' + createIds.length + ' '
                            + this.$fmt.plural(createIds.length, 'товар', 'товара', 'товаров') + '.',
                        details: [
                            chosen.length > createIds.length
                                ? 'Остальные выбранные товары не войдут в этот запуск.'
                                : 'Запускается только создание карточек, ещё не связанных с WB.',
                            'Для ожидающих или ошибочных товаров сначала проверим готовность.',
                            'При создании отправим рассчитанную цену, если она доступна, и доступные фото.',
                            'Отправка идёт в фоне, за ней можно следить здесь',
                        ],
                        label: 'Создать карточки',
                    },
                    refresh_supplier: {
                        title: 'Обновить из поставщика',
                        message: 'Обновим ' + chosen.length + ' '
                            + this.$fmt.plural(chosen.length, 'карточку', 'карточки', 'карточек')
                            + ' данными из каталога поставщика.',
                        details: [
                            'Изменятся только данные в Seller Hub; карточки на площадках не меняются.',
                            'Товары без связи с каталогом поставщика будут отмечены отдельно.',
                        ],
                        label: 'Обновить',
                    },
                    audit: {
                        title: 'Сверить с Wildberries',
                        message: 'Проверим, что реально лежит на WB у ' + published + ' '
                            + this.$fmt.plural(published, 'карточки', 'карточек', 'карточек') + '.',
                        details: [
                            'Только чтение: ничего не перезаписываем',
                            'Расхождения покажем бейджами в списке',
                        ],
                        label: 'Сверить',
                    },
                    delete: {
                        title: 'Удалить из «Моих товаров»',
                        message: 'Удалим ' + chosen.length + ' '
                            + this.$fmt.plural(chosen.length, 'карточку', 'карточки', 'карточек')
                            + ' из вашего списка.',
                        details: published
                            ? [
                                published + ' уже опубликованы на маркетплейсе — они там останутся продаваться',
                                'Вы потеряете управление ими из Seller Hub',
                                'Товары снова станут доступны для импорта из каталога',
                            ]
                            : ['Товары снова станут доступны для импорта из каталога'],
                        label: 'Удалить',
                        danger: true,
                    },
                };
                if (action.key === 'enrich') {
                    var enrichIds = exactWbProductIds(chosen);
                    if (!enrichIds || enrichIds.length > MAX_BULK_SELECTION) {
                        this.error = 'Для изменения выберите только опубликованные карточки WB, не больше 200.';
                        return;
                    }
                    // Существующий review route принимает POST selected_ids с WB Product ID.
                    this.postSelectionForm(this.urls.enrichBulk, enrichIds);
                    return;
                }
                if (action.key === 'ozon') {
                    if (this.ozonPending || this.ozonStorageProblem || this.ozonBusy || this.ozonSessionEnded) return;
                    if (!selectionIds.length || selectionIds.length > MAX_BULK_SELECTION) {
                        this.ozonError = 'Выберите от 1 до 200 товаров для одной подготовки.';
                        return;
                    }
                    this.ozonIds = selectionIds.slice();
                    this.ozonAccountId = this.ozonAccounts.length === 1 ? String(this.ozonAccounts[0].id) : '';
                    this.ozonConfirmed = false;
                    this.ozonError = '';
                    this.$nextTick(function () {
                        this.$refs.ozonDialog.showModal();
                        this.$refs.ozonAccount.focus();
                    });
                    return;
                }
                if (action.key === 'common_content') {
                    var productIds = selectionIds.slice();
                    if (productIds.length > 50) {
                        this.error = 'За один раз можно открыть не больше 50 товаров. Снимите часть отметок.';
                        return;
                    }
                    try {
                        window.location.href = window.SellerHubCommonContent.selectionUrl(this.urls.commonContent, productIds);
                    } catch (_) {
                        this.error = 'Не удалось открыть выбор товаров. Обновите страницу и повторите действие.';
                    }
                    return;
                }
                if (action.key === 'wb_prices') {
                    var priceIds = exactWbProductIds(chosen);
                    if (!priceIds || priceIds.length > MAX_BULK_SELECTION || !this.wbConnected) {
                        this.error = 'Для изменения цены выберите до 200 карточек WB и проверьте подключение кабинета.';
                        return;
                    }
                    window.location.assign(this.urls.wbPriceChange + '?product_ids=' + encodeURIComponent(priceIds.join(',')));
                    return;
                }
                var plan = plans[action.key];
                if (!plan) return;
                var effectIds = action.key === 'push_wb' ? createIds : selectionIds;
                if (!effectIds.length || effectIds.length > MAX_BULK_SELECTION) {
                    this.error = 'Для одного запуска выберите допустимый набор товаров и повторите действие.';
                    return;
                }
                this.confirm = {
                    open: true,
                    key: action.key,
                    title: plan.title,
                    message: plan.message,
                    details: plan.details,
                    label: plan.label,
                    danger: !!plan.danger,
                    busy: false,
                    productIds: effectIds.slice(),
                    selectionIds: selectionIds.slice(),
                    labels: selectedProductLabels(this.items, effectIds),
                };
                if (action.key === 'refresh_supplier') this.supplierRefreshResult = null;
            },
            postSelectionForm: function (url, productIds) {
                var csrf = document.querySelector('meta[name="csrf-token"]')?.content;
                if (!csrf) {
                    this.error = 'Защита формы недоступна. Обновите страницу и повторите действие.';
                    return;
                }
                var form = document.createElement('form');
                form.method = 'post';
                form.action = url;
                var csrfInput = document.createElement('input');
                csrfInput.type = 'hidden';
                csrfInput.name = 'csrf_token';
                csrfInput.value = csrf;
                form.appendChild(csrfInput);
                productIds.forEach(function (id) {
                    var input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = 'selected_ids';
                    input.value = String(id);
                    form.appendChild(input);
                });
                document.body.appendChild(form);
                form.submit();
            },
            cancelAction: function () {
                if (this.confirm.busy) return;
                this.confirm.open = false;
                this.confirm.productIds = [];
                this.confirm.selectionIds = [];
                this.confirm.labels = [];
            },
            runAction: function () {
                var self = this;
                if (this.confirm.busy || !this.confirm.open) return;
                var key = this.confirm.key;
                var ids = Array.isArray(this.confirm.productIds) ? this.confirm.productIds.slice() : [];
                var selectionAtReview = Array.isArray(this.confirm.selectionIds) ? this.confirm.selectionIds.slice() : [];
                var labelsAtReview = Array.isArray(this.confirm.labels)
                    ? this.confirm.labels.map(function (entry) { return Object.assign({}, entry); }) : [];
                if (!key || !ids.length || ids.length > MAX_BULK_SELECTION) return;
                if (!sameIds(this.selected, selectionAtReview)) {
                    this.confirm.open = false;
                    this.confirm.productIds = [];
                    this.confirm.selectionIds = [];
                    this.confirm.labels = [];
                    this.error = 'Выбор изменился после проверки. Отметьте нужные товары и откройте действие заново.';
                    return;
                }
                this.confirm.busy = true;
                if (key === 'refresh_supplier') this.supplierRefreshResult = null;

                var request;
                if (key === 'push_wb') {
                    this.jobTitle = 'Отправляем на Wildberries';
                    request = this.$api.post(this.urls.pushToWb, { product_ids: ids });
                } else if (key === 'refresh_supplier') {
                    request = this.$api.post(this.urls.refreshFromSupplier, { product_ids: ids });
                } else if (key === 'audit') {
                    this.jobTitle = 'Сверяем с Wildberries';
                    request = this.$api.post(this.urls.wbAudit, { product_ids: ids });
                } else if (key === 'delete') {
                    this.jobTitle = 'Удаляем';
                    request = this.$api.post(this.urls.deleteBulk, { product_ids: ids });
                }
                if (!request) { this.confirm.busy = false; return; }

                request.then(function (data) {
                    self.confirm.open = false;
                    if (key === 'refresh_supplier') {
                        var outcome = supplierRefreshOutcome(data, ids, labelsAtReview, self.urls.commonContent || '');
                        if (sameIds(self.selected, selectionAtReview)) self.selected = [];
                        if (outcome) {
                            self.supplierRefreshResult = outcome;
                            self.load();
                            self.loadFacets();
                        } else {
                            self.supplierRefreshResult = supplierRefreshNotice(
                                'unknown', labelsAtReview.length,
                                'Ответ не подтвердил итог. Обновите список перед следующим запуском.',
                            );
                        }
                        self.$nextTick(function () {
                            self.$refs.supplierRefreshResult?.focus();
                        });
                        return;
                    }
                    if (sameIds(self.selected, selectionAtReview)) self.selected = [];
                    if (data.job_uid || data.job_id) {
                        self.trackJob(data.job_uid || data.job_id, ids.length);
                    } else {
                        var updated = typeof data.updated === 'number' ? data.updated : null;
                        var deleted = typeof data.deleted === 'number' ? data.deleted : null;
                        var skipped = typeof data.skipped === 'number' ? data.skipped : 0;
                        var succeeded = updated !== null ? updated : (deleted !== null ? deleted : ids.length);
                        var processed = updated !== null ? updated + skipped : ids.length;
                        // Синхронное действие: показываем итог сразу
                        self.job = {
                            status: 'done',
                            total: ids.length,
                            processed: processed,
                            succeeded: succeeded,
                            skipped: skipped,
                            failed: typeof data.errors === 'number' ? data.errors : 0,
                            message: data.message || '',
                        };
                        self.load();
                        self.loadFacets();
                    }
                }).catch(function (err) {
                    self.confirm.open = false;
                    if (key === 'refresh_supplier') {
                        var rejected = Number.isInteger(err && err.status) && err.status >= 400 && err.status < 500 &&
                            !(err && err.code === 'auth_required');
                        if (sameIds(self.selected, selectionAtReview)) self.selected = [];
                        self.supplierRefreshResult = supplierRefreshNotice(
                            rejected ? 'rejected' : 'unknown',
                            labelsAtReview.length,
                            rejected
                                ? 'Сервер отклонил запрос. Обновите список и выберите товары заново.'
                                : 'Сервер не подтвердил итог. Обновите список перед следующим запуском.',
                        );
                        self.$nextTick(function () {
                            self.$refs.supplierRefreshResult?.focus();
                        });
                    } else {
                        self.error = err.message || 'Действие не выполнено';
                    }
                }).finally(function () {
                    self.confirm.busy = false;
                    self.confirm.productIds = [];
                    self.confirm.selectionIds = [];
                    self.confirm.labels = [];
                });
            },
            refreshListAfterSupplierResult: function () {
                this.load();
                this.loadFacets();
            },
            closeOzonDialog: function () {
                if (this.ozonBusy) return;
                this.$refs.ozonDialog.close();
                this.ozonConfirmed = false;
            },
            clearOzonStorageProblem: function () {
                if (!this.ozonStorageProblem || this.ozonBusy) return;
                clearOzonPending(); this.ozonStorageProblem = false;
                this.ozonError = 'Повреждённая запись снята после проверки истории. Выберите товары заново перед подготовкой.';
            },
            prepareOzon: async function () {
                var accountId = Number(this.ozonAccountId);
                if (this.ozonBusy || this.ozonPending || this.ozonStorageProblem || this.ozonSessionEnded ||
                    !this.ozonConfirmed || !Number.isSafeInteger(accountId) ||
                    !this.ozonAccounts.some(function (row) { return row.id === accountId; }) ||
                    !this.ozonIds.length || this.ozonIds.length > 200) return;
                var csrf = document.querySelector('meta[name="csrf-token"]')?.content;
                if (!csrf) { this.ozonError = 'Защита формы недоступна. Обновите страницу перед подготовкой.'; return; }
                var pending = {account_id:accountId, imported_product_ids:this.ozonIds.slice(),
                    request_key:ozonRequestKey()};
                if (!saveOzonPending(pending)) {
                    this.ozonError = 'Браузер не сохранил ключ подготовки. Запуск остановлен, чтобы не потерять результат.';
                    return;
                }
                this.ozonPending = pending;
                await this.submitOzonPending(pending);
            },
            retryOzonPending: async function () {
                if (!this.ozonPending || !this.ozonRetrySameKey || this.ozonBusy ||
                    this.ozonSessionEnded || this.ozonStorageProblem) return;
                this.ozonRetrySameKey = false;
                await this.submitOzonPending(this.ozonPending);
            },
            submitOzonPending: async function (pending) {
                if (!validOzonPending(pending, this.ozonAccounts) || this.ozonBusy || this.ozonSessionEnded) return;
                var csrf = document.querySelector('meta[name="csrf-token"]')?.content;
                if (!csrf) { this.ozonError = 'Защита формы недоступна. Обновите страницу перед подготовкой.'; return; }
                this.ozonBusy = 'post'; this.ozonError = '';
                var controller = new AbortController();
                var timer = setTimeout(function () { controller.abort(); }, 45000);
                try {
                    var response = await fetch(this.urls.ozonUploads, {
                        method:'POST', credentials:'same-origin', redirect:'manual', signal:controller.signal,
                        headers:{Accept:'application/json','Content-Type':'application/json','X-CSRFToken':csrf},
                        body:JSON.stringify({...pending, confirm_prepare:true}),
                    });
                    if (response.status === 401 || response.type === 'opaqueredirect' || response.redirected)
                        throw Object.assign(Error('Сессия завершилась. Войдите снова и проверьте результат подготовки.'), {status:401});
                    if (response.status === 403)
                        throw Object.assign(Error('Доступ к выбранному магазину больше недоступен.'), {status:403});
                    if (!(response.headers.get('content-type') || '').includes('application/json'))
                        throw Error('Ответ подготовки не подтверждён. Проверьте результат по сохранённому ключу.');
                    var data = await response.json();
                    if (!response.ok || data.success !== true) {
                        if (response.status < 500) { clearOzonPending(); this.ozonPending = null; }
                        throw Object.assign(Error(data.error || 'Подготовка не принята.'), {status:response.status});
                    }
                    if (data.run?.mode !== 'source_prepare' || data.run.account_id !== pending.account_id ||
                        !/^ozon-upload-[0-9a-f]{32}$/.test(data.run.job_uid))
                        throw Error('Ответ не совпал с подготовкой. Проверьте результат по ключу.');
                    clearOzonPending(); this.ozonPending = null;
                    window.location.assign(this.urls.ozonUploads + data.run.job_uid);
                } catch (error) {
                    if (error.status === 401 || error.status === 403) this.ozonSessionEnded = true;
                    this.ozonError = error.name === 'AbortError'
                        ? 'Ответ не пришёл вовремя. Повторная подготовка заблокирована; проверьте результат.'
                        : error instanceof TypeError
                            ? 'Связь прервалась. Повторная подготовка заблокирована; проверьте результат.'
                            : error.message;
                } finally {
                    clearTimeout(timer); this.ozonBusy = '';
                    this.$refs.ozonDialog.close();
                }
            },
            readOzonPending: async function () {
                if (!this.ozonPending || this.ozonBusy || this.ozonSessionEnded) return;
                this.ozonBusy = 'read'; this.ozonError = ''; this.ozonRetrySameKey = false;
                var controller = new AbortController();
                var timer = setTimeout(function () { controller.abort(); }, 10000);
                try {
                    var response = await fetch(this.urls.ozonByRequest + '?account_id=' + this.ozonPending.account_id, {
                        credentials:'same-origin', cache:'no-store', redirect:'manual', signal:controller.signal,
                        headers:{Accept:'application/json','X-Upload-Request-Key':this.ozonPending.request_key},
                    });
                    if (response.status === 401 || response.type === 'opaqueredirect' || response.redirected)
                        throw Object.assign(Error('Сессия завершилась. Войдите снова для проверки.'), {status:401});
                    if (response.status === 403)
                        throw Object.assign(Error('Доступ к магазину больше недоступен.'), {status:403});
                    if (!(response.headers.get('content-type') || '').includes('application/json'))
                        throw Error('Ответ проверки не подтверждён.');
                    var data = await response.json();
                    if (typeof data.csrf_token === 'string' && data.csrf_token)
                        document.querySelector('meta[name="csrf-token"]')?.setAttribute('content', data.csrf_token);
                    if (response.status === 404)
                        throw Object.assign(Error('Запуск по сохранённому ключу не найден. Это не доказывает, что первый запрос не был принят; можно вручную повторить только тот же запрос.'), {status:404});
                    if (!response.ok || data.success !== true) throw Object.assign(Error(data.error || 'Не удалось проверить запуск.'), {status:response.status});
                    if (data.run?.mode !== 'source_prepare' || data.run.account_id !== this.ozonPending.account_id ||
                        !/^ozon-upload-[0-9a-f]{32}$/.test(data.run.job_uid))
                        throw Error('Найденный запуск не совпал с выбранным магазином.');
                    clearOzonPending(); this.ozonPending = null;
                    window.location.assign(this.urls.ozonUploads + data.run.job_uid);
                } catch (error) {
                    if (error.status === 401 || error.status === 403) this.ozonSessionEnded = true;
                    if (error.status === 404) this.ozonRetrySameKey = true;
                    this.ozonError = error.name === 'AbortError' ? 'Проверка заняла слишком много времени. Повторите только чтение.' :
                        error instanceof TypeError ? 'Нет связи. Повторите только чтение.' : error.message;
                } finally { clearTimeout(timer); this.ozonBusy = ''; }
            },
            trackJob: function (jobUid, total) {
                var self = this;
                if (this._jobTimer) clearInterval(this._jobTimer);
                var poll = function () {
                    // Вкладка скрыта — не жжём запросы
                    if (document.hidden) return;
                    self.$api.get(self.urls.jobStatus + jobUid).then(function (data) {
                        var job = data.job || data;
                        self.job = job;
                        if (job.status === 'done' || job.status === 'failed') {
                            clearInterval(self._jobTimer);
                            self._jobTimer = null;
                            self.load();
                            self.loadFacets();
                        }
                    }).catch(function () {
                        clearInterval(self._jobTimer);
                        self._jobTimer = null;
                    });
                };
                this.job = { status: 'running', total: total || 0, processed: 0 };
                poll();
                this._jobTimer = setInterval(poll, 2500);
            },
            retryFailed: function () {
                // Повтор доступен только после честного итога с ошибками
                this.job = null;
                this.load();
            },

            /* ---------- отображение ---------- */
            letterOf: function (item) {
                var title = ((item && item.title) || '').trim();
                return title ? title[0].toUpperCase() : '·';
            },
            scoreTone: function (score) {
                if (score === null || score === undefined) return 'muted';
                if (score >= 70) return 'ok';
                if (score >= 50) return 'warn';
                return 'danger';
            },
            wbTitle: function (item) {
                var parts = [];
                if (item.wb.nm_id) parts.push('Артикул WB ' + item.wb.nm_id);
                if (item.wb.quantity !== null) parts.push('остаток ' + item.wb.quantity);
                if (item.wb.rating) parts.push('рейтинг ' + item.wb.rating);
                return parts.join(' · ');
            },
            reasonsTitle: function (item) {
                var reasons = (item.wb && item.wb.attention_reasons) || [];
                var labels = {
                    few_photos: 'мало фото',
                    weak_chars: 'пустые характеристики',
                    weak_description: 'слабое описание',
                    weak_title: 'слабое название',
                    low_cart_conv: 'не кладут в корзину',
                    low_buyout: 'низкий выкуп',
                    low_rating: 'низкий рейтинг',
                    no_views: 'не видят покупатели',
                };
                if (!reasons.length) return 'Оценка карточки';
                return 'Что мешает: ' + reasons.map(function (code) {
                    return labels[code] || code;
                }).join(', ');
            },
        },
        watch: {
            ozonError: function (value) {
                if (value) this.$nextTick(function () { this.$refs.ozonError?.focus(); });
            },
        },
    });
})();
