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
    var MAX_OZON_PRICE_HANDOFF = 100;
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

    function sameOrderedIds(left, right) {
        return Array.isArray(left) && Array.isArray(right) && left.length === right.length &&
            left.every(function (id, index) { return id === right[index]; });
    }

    function orderedProductIds(items) {
        if (!Array.isArray(items)) return null;
        var ids = [];
        var seen = new Set();
        for (var i = 0; i < items.length; i += 1) {
            var id = items[i] && items[i].id;
            if (!Number.isSafeInteger(id) || id <= 0 || seen.has(id)) return null;
            seen.add(id);
            ids.push(id);
        }
        return ids;
    }

    function exactOzonPriceAccountChoices(items, selectionIds, accounts) {
        if (!Array.isArray(selectionIds) || !selectionIds.length ||
            selectionIds.length > MAX_OZON_PRICE_HANDOFF) {
            return {
                choices: [],
                reason: Array.isArray(selectionIds) && selectionIds.length > MAX_OZON_PRICE_HANDOFF
                    ? 'limit' : 'selection',
            };
        }
        if (selectionIds.some(function (id) {
            return !Number.isSafeInteger(id) || id <= 0;
        }) || new Set(selectionIds).size !== selectionIds.length ||
            !Array.isArray(items) || items.length !== selectionIds.length) {
            return { choices: [], reason: 'selection' };
        }

        var itemsById = new Map();
        for (var i = 0; i < items.length; i += 1) {
            var item = items[i];
            if (!item || !Number.isSafeInteger(item.id) || item.id <= 0 ||
                itemsById.has(item.id)) return { choices: [], reason: 'selection' };
            itemsById.set(item.id, item);
        }
        if (!sameIds(selectionIds, Array.from(itemsById.keys()))) {
            return { choices: [], reason: 'selection' };
        }

        if (!Array.isArray(accounts)) return { choices: [], reason: 'accounts' };
        var accountIds = new Set();
        var activeAccounts = [];
        for (var a = 0; a < accounts.length; a += 1) {
            var account = accounts[a];
            if (!account || !Number.isSafeInteger(account.id) || account.id <= 0 ||
                accountIds.has(account.id)) return { choices: [], reason: 'accounts' };
            accountIds.add(account.id);
            // The page bootstrap is already seller/Ozon/active scoped. Honor
            // explicit status fields too when a newer DTO supplies them.
            if ((account.is_active !== undefined && account.is_active !== true) ||
                (account.marketplace_code !== undefined &&
                    account.marketplace_code !== 'ozon') ||
                (account.connection_status !== undefined &&
                    account.connection_status !== 'connected')) continue;
            activeAccounts.push(account);
        }
        if (!activeAccounts.length) return { choices: [], reason: 'no_accounts' };

        var choices = [];
        activeAccounts.forEach(function (account) {
            var listingIds = [];
            var eligible = true;
            selectionIds.forEach(function (productId) {
                if (!eligible) return;
                var product = itemsById.get(productId);
                var listings = product && product.ozon && product.ozon.listings;
                if (!Array.isArray(listings)) {
                    eligible = false;
                    return;
                }
                var accountRows = listings.filter(function (listing) {
                    return listing && listing.account_id === account.id;
                });
                if (accountRows.length !== 1) {
                    eligible = false;
                    return;
                }
                var listing = accountRows[0];
                if (listing.marketplace_code !== 'ozon' ||
                    listing.link_status !== 'linked' ||
                    listing.account_is_active !== true ||
                    listing.is_available !== true ||
                    listing.is_archived !== false ||
                    !Number.isSafeInteger(listing.id) || listing.id <= 0) {
                    eligible = false;
                    return;
                }
                listingIds.push(listing.id);
            });
            if (eligible && listingIds.length === selectionIds.length &&
                new Set(listingIds).size === listingIds.length) {
                choices.push({
                    account_id: account.id,
                    account_label: typeof account.label === 'string' ? account.label : '',
                    listing_ids: listingIds,
                });
            }
        });
        return {
            choices: choices,
            reason: choices.length ? null : 'no_common_account',
        };
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
                itemsScope: '',
                facets: {},
                pagination: {},
                loading: true,
                loadingMore: false,
                selectAllLoading: false,
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
                ozonReturnFocus: null,
                ozonPending: validOzonPending(savedOzon, boot.ozonAccounts || []) ? savedOzon : null,
                ozonStorageProblem: hasOzonPending() && !validOzonPending(savedOzon, boot.ozonAccounts || []),
                ozonRetrySameKey: false,
                ozonSessionEnded: false,
                ozonPriceError: '',
                ozonPriceAccountId: '',
                ozonPriceHandoffReview: null,
                ozonPriceDialogError: '',
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
                var chosen = this.selected;
                return this.items.length > 0 && this.items.every(function (item) {
                    return chosen.indexOf(item.id) !== -1;
                });
            },
            someSelected: function () {
                var chosen = this.selected;
                var visibleSelected = this.items.filter(function (item) {
                    return chosen.indexOf(item.id) !== -1;
                }).length;
                return visibleSelected > 0 && visibleSelected < this.items.length;
            },
            selectedItems: function () {
                var chosen = this.selected;
                return this.items.filter(function (item) {
                    return chosen.indexOf(item.id) !== -1;
                });
            },
            hasUnshownResults: function () {
                return this.itemsScope === this.feedScopeKey() &&
                    Number.isSafeInteger(this.pagination.total) &&
                    this.pagination.total > this.items.length;
            },
            canSelectAllFiltered: function () {
                return !this.loading && !this.loadingMore &&
                    this.itemsScope === this.feedScopeKey() &&
                    (this.searchInput || '').trim().slice(0, 200) === this.filters.search &&
                    Number.isSafeInteger(this.pagination.total) &&
                    this.pagination.total > this.items.length &&
                    this.pagination.total > 0 &&
                    this.pagination.total <= MAX_BULK_SELECTION;
            },
            supplierRefreshOzonIds: function () {
                var result = this.supplierRefreshResult;
                if (!result || !['complete', 'partial'].includes(result.kind) ||
                    !Array.isArray(result.entries) ||
                    !Number.isSafeInteger(result.requested) || result.requested <= 0 ||
                    result.entries.length !== result.requested ||
                    !Number.isSafeInteger(result.updated) || result.updated < 0 ||
                    !Number.isSafeInteger(result.skipped) || result.skipped < 0 ||
                    !Number.isSafeInteger(result.errors) || result.errors < 0 ||
                    result.updated + result.skipped + result.errors !== result.requested ||
                    (result.kind === 'partial') !== (result.errors > 0)) return [];

                var seen = new Set();
                var updatedIds = [];
                for (var i = 0; i < result.entries.length; i += 1) {
                    var entry = result.entries[i];
                    if (!entry || !Number.isSafeInteger(entry.productId) ||
                        entry.productId <= 0 || seen.has(entry.productId) ||
                        !['updated', 'skipped', 'error'].includes(entry.status)) return [];
                    seen.add(entry.productId);
                    if (entry.status === 'updated') updatedIds.push(entry.productId);
                }
                return updatedIds.length === result.updated ? updatedIds : [];
            },
            ozonPreparationDisabled: function () {
                return !this.ozonAccounts.length || !!this.ozonPending ||
                    this.ozonStorageProblem || !!this.ozonBusy || this.ozonSessionEnded;
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
                            disabled: !underLimit || this.ozonPreparationDisabled,
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
                        this.ozonEnabled ? {
                            key: 'ozon_prices',
                            label: 'Изменить цены Ozon',
                            hint: !chosen.length
                                ? 'выберите товары'
                                : chosen.length > MAX_OZON_PRICE_HANDOFF
                                    ? 'за одно сравнение можно выбрать не больше 100 товаров'
                                    : 'откроется сравнение для одного магазина, где есть все выбранные карточки',
                            disabled: !chosen.length || chosen.length > MAX_OZON_PRICE_HANDOFF,
                        } : null,
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
                var next = (value || '').trim().slice(0, 200);
                if (next !== this.filters.search) {
                    this.cancelFilteredSelect();
                    this.selected = [];
                }
                clearTimeout(this._searchTimer);
                this._searchTimer = setTimeout(function () {
                    if (next !== self.filters.search) self.filters.search = next;
                }, 300);
            },
            filters: { deep: true, handler: function () { this.load(); } },
            selected: {
                deep: true,
                handler: function (next) {
                    if (this._selectAllAbort &&
                        !sameOrderedIds(next, this._selectAllSelectionSnapshot)) {
                        this.cancelFilteredSelect();
                    }
                },
            },
            ozonError: function (value) {
                if (value) this.$nextTick(function () { this.$refs.ozonError?.focus(); });
            },
        },

        mounted: function () {
            this.load();
            this.loadFacets();
        },
        beforeUnmount: function () {
            if (this._jobTimer) clearInterval(this._jobTimer);
            if (this._abort) this._abort.abort();
            if (this._loadMoreAbort) this._loadMoreAbort.abort();
            this.cancelFilteredSelect();
        },

        methods: {
            queryParams: function (page, perPage) {
                var params = {
                    search: this.filters.search,
                    supplier: this.filters.supplier,
                    has_photos: this.filters.has_photos,
                    stock: this.filters.stock,
                    sort: this.filters.sort,
                    page: page || 1,
                    per_page: perPage || 50,
                };
                if (this.activeTab === 'updates') params.updates = '1';
                else if (this.activeTab) params.status = this.activeTab;
                return params;
            },
            feedScopeKey: function () {
                var params = this.queryParams(1, 50);
                delete params.page;
                delete params.per_page;
                return JSON.stringify(params);
            },
            cancelFilteredSelect: function () {
                if (this._selectAllAbort) this._selectAllAbort.abort();
                this._selectAllAbort = null;
                this._selectAllSelectionSnapshot = null;
                this._selectAllRevision = (this._selectAllRevision || 0) + 1;
                this.selectAllLoading = false;
            },
            load: function () {
                var self = this;
                if (this._abort) this._abort.abort();
                if (this._loadMoreAbort) this._loadMoreAbort.abort();
                this._loadMoreAbort = null;
                this.loadingMore = false;
                this.cancelFilteredSelect();
                this.selected = [];
                var revision = (this._feedRevision || 0) + 1;
                this._feedRevision = revision;
                var scope = this.feedScopeKey();
                var abort = new AbortController();
                this._abort = abort;
                this.loading = true;
                this.error = '';
                this.$api.get(this.urls.feed, this.queryParams(1), { signal: abort.signal })
                    .then(function (data) {
                        if (abort.signal.aborted || self._feedRevision !== revision) return;
                        self.items = data.items || [];
                        self.pagination = data.pagination || {};
                        self.itemsScope = scope;
                    }).catch(function (err) {
                        if ((err && err.name === 'AbortError') || abort.signal.aborted ||
                            self._feedRevision !== revision) return;
                        self.error = err.message || 'Не удалось загрузить товары';
                    }).finally(function () {
                        if (self._abort === abort) {
                            self._abort = null;
                            self.loading = false;
                        }
                    });
            },
            loadMore: function () {
                if (this.loading || this.loadingMore || !this.pagination.has_next ||
                    this.itemsScope !== this.feedScopeKey()) return;
                var self = this;
                var revision = this._feedRevision || 0;
                var scope = this.feedScopeKey();
                var currentPage = this.pagination.page;
                var total = this.pagination.total;
                var perPage = this.pagination.per_page;
                if (!Number.isSafeInteger(currentPage) || currentPage < 1 ||
                    !Number.isSafeInteger(total) || total < 0 ||
                    !Number.isSafeInteger(perPage) || perPage < 1) {
                    this.pagination = Object.assign({}, this.pagination, { has_next: false });
                    this.error = 'Список изменился. Обновите его перед продолжением.';
                    return;
                }
                var nextPage = currentPage + 1;
                var abort = new AbortController();
                this._loadMoreAbort = abort;
                this.loadingMore = true;
                this.$api.get(this.urls.feed, this.queryParams(nextPage), { signal: abort.signal })
                    .then(function (data) {
                        if (abort.signal.aborted || self._feedRevision !== revision ||
                            self.feedScopeKey() !== scope) return;
                        var pageIds = data && orderedProductIds(data.items);
                        var existingIds = orderedProductIds(self.items);
                        var expectedCount = Math.min(perPage, Math.max(0, total - currentPage * perPage));
                        var consistent = self.pagination.page === currentPage &&
                            self.pagination.total === total && data && data.pagination &&
                            data.pagination.page === nextPage &&
                            data.pagination.per_page === perPage && data.pagination.total === total &&
                            typeof data.pagination.has_next === 'boolean' &&
                            Array.isArray(data.items) && data.items.length === expectedCount &&
                            pageIds && existingIds &&
                            !pageIds.some(function (id) { return existingIds.indexOf(id) !== -1; });
                        if (!consistent) {
                            self.pagination = Object.assign({}, self.pagination, { has_next: false });
                            self.error = 'Список изменился. Обновите его перед продолжением.';
                            return;
                        }
                        var seen = {};
                        self.items.forEach(function (item) { seen[item.id] = true; });
                        (data.items || []).forEach(function (item) {
                            if (!seen[item.id]) self.items.push(item);
                        });
                        self.pagination = data.pagination || self.pagination;
                    }).catch(function (err) {
                        if ((err && err.name === 'AbortError') || abort.signal.aborted ||
                            self._feedRevision !== revision || self.feedScopeKey() !== scope) return;
                        self.error = err.message || 'Не удалось загрузить ещё';
                    }).finally(function () {
                        if (self._loadMoreAbort === abort) {
                            self._loadMoreAbort = null;
                            self.loadingMore = false;
                        }
                    });
            },
            selectAllFiltered: function () {
                if (!this.canSelectAllFiltered || this.selectAllLoading) return;
                var self = this;
                var total = this.pagination.total;
                var scope = this.feedScopeKey();
                if (this.itemsScope !== scope) return;
                var shownIds = orderedProductIds(this.items);
                var selectionSnapshot = this.selected.slice();
                if (!shownIds || new Set(selectionSnapshot).size !== selectionSnapshot.length) {
                    this.error = 'Не удалось проверить текущий список. Обновите его и повторите выбор.';
                    return;
                }
                var params = this.queryParams(1, MAX_BULK_SELECTION);
                var revision = (this._selectAllRevision || 0) + 1;
                this._selectAllRevision = revision;
                var abort = new AbortController();
                this._selectAllAbort = abort;
                this._selectAllSelectionSnapshot = selectionSnapshot;
                this.selectAllLoading = true;
                this.error = '';
                this.$api.get(this.urls.feed, params, { signal: abort.signal })
                    .then(function (data) {
                        if (abort.signal.aborted || self._selectAllAbort !== abort ||
                            self._selectAllRevision !== revision || self.feedScopeKey() !== scope ||
                            (self.searchInput || '').trim().slice(0, 200) !== self.filters.search ||
                            !sameOrderedIds(self.selected, selectionSnapshot)) return;

                        var fullIds = data && orderedProductIds(data.items);
                        var responsePage = data && data.pagination;
                        var currentShownIds = orderedProductIds(self.items);
                        var valid = data && data.success === true && Array.isArray(data.items) &&
                            responsePage && responsePage.page === 1 &&
                            responsePage.per_page === MAX_BULK_SELECTION &&
                            responsePage.total === total && responsePage.has_next === false &&
                            data.items.length === total && fullIds && fullIds.length === total &&
                            sameOrderedIds(shownIds, fullIds.slice(0, shownIds.length)) &&
                            currentShownIds &&
                            sameOrderedIds(currentShownIds, fullIds.slice(0, currentShownIds.length));
                        if (!valid) {
                            self.error = 'Список изменился или не загрузился целиком. Обновите его и повторите выбор.';
                            return;
                        }

                        self._selectAllAbort = null;
                        self._selectAllSelectionSnapshot = null;
                        self.selectAllLoading = false;
                        if (self._loadMoreAbort) self._loadMoreAbort.abort();
                        self._loadMoreAbort = null;
                        self.loadingMore = false;
                        self._feedRevision = (self._feedRevision || 0) + 1;
                        self.items = data.items;
                        self.pagination = responsePage;
                        self.itemsScope = scope;
                        self.selected = fullIds.slice();
                    }).catch(function (err) {
                        if ((err && err.name === 'AbortError') || abort.signal.aborted ||
                            self._selectAllAbort !== abort || self._selectAllRevision !== revision ||
                            self.feedScopeKey() !== scope) return;
                        self.error = err.message || 'Не удалось выбрать весь результат. Повторите попытку.';
                    }).finally(function () {
                        if (self._selectAllAbort === abort) {
                            self._selectAllAbort = null;
                            self._selectAllSelectionSnapshot = null;
                            self.selectAllLoading = false;
                        }
                    });
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
            resolveOzonPriceAccountChoices: function (items, selectionIds) {
                return exactOzonPriceAccountChoices(items, selectionIds, this.ozonAccounts);
            },
            setOzonPriceHandoffError: function (message) {
                this.ozonPriceError = message;
                this.$nextTick(function () {
                    this.$refs.ozonPriceError?.focus();
                });
            },
            openOzonPriceHandoff: function (items, selectionIds) {
                this.ozonPriceError = '';
                this.ozonPriceDialogError = '';
                if (!sameIds(this.selected, selectionIds)) {
                    this.setOzonPriceHandoffError(
                        'Выбор товаров изменился. Отметьте товары заново и повторите действие.'
                    );
                    return;
                }
                var result = this.resolveOzonPriceAccountChoices(items, selectionIds);
                if (!result.choices.length) {
                    var message = result.reason === 'limit'
                        ? 'За одно сравнение Ozon можно выбрать не больше 100 товаров.'
                        : result.reason === 'no_accounts'
                            ? 'Нет активного магазина Ozon. Подключите магазин или откройте «Ценовые заявки Ozon».'
                            : result.reason === 'selection'
                                ? 'Список товаров изменился или неполон. Обновите его и отметьте товары заново.'
                                : result.reason === 'accounts'
                                    ? 'Не удалось проверить список магазинов Ozon. Обновите страницу и повторите.'
                                    : 'Не найден один магазин Ozon с точной связанной, доступной и неархивной карточкой для каждого выбранного товара. Выбор не сокращён. Проверьте карточки или откройте «Ценовые заявки Ozon».';
                    this.setOzonPriceHandoffError(message);
                    return;
                }
                var review = {
                    imported_product_ids: selectionIds.slice(),
                    choices: result.choices.map(function (choice) {
                        return {
                            account_id: choice.account_id,
                            account_label: choice.account_label,
                            listing_ids: choice.listing_ids.slice(),
                        };
                    }),
                };
                this.ozonPriceHandoffReview = review;
                if (review.choices.length === 1) {
                    this.navigateOzonPriceHandoff(review.choices[0].account_id, review);
                    return;
                }
                this._ozonPriceTrigger = document.activeElement;
                this.ozonPriceAccountId = '';
                this.$nextTick(function () {
                    this.$refs.ozonPriceAccountDialog.showModal();
                    this.$refs.ozonPriceAccount.focus();
                });
            },
            closeOzonPriceHandoff: function () {
                var dialog = this.$refs.ozonPriceAccountDialog;
                if (dialog && dialog.open) dialog.close();
                this.ozonPriceHandoffReview = null;
                this.ozonPriceAccountId = '';
                this.ozonPriceDialogError = '';
                this._ozonPriceTrigger?.focus();
            },
            continueOzonPriceHandoff: function () {
                var review = this.ozonPriceHandoffReview;
                var accountId = Number(this.ozonPriceAccountId);
                if (!review || !Number.isSafeInteger(accountId) || accountId <= 0 ||
                    !review.choices.some(function (choice) {
                        return choice.account_id === accountId;
                    })) {
                    this.ozonPriceDialogError = 'Выберите магазин из списка.';
                    this.$nextTick(function () {
                        this.$refs.ozonPriceAccount.focus();
                    });
                    return;
                }
                this.navigateOzonPriceHandoff(accountId, review);
            },
            navigateOzonPriceHandoff: function (accountId, review) {
                if (!review || !sameIds(this.selected, review.imported_product_ids)) {
                    this.closeOzonPriceHandoff();
                    this.setOzonPriceHandoffError(
                        'Выбор товаров изменился после проверки. Отметьте их заново перед сравнением.'
                    );
                    return false;
                }
                var selectedItems = this.selectedItems;
                var latest = this.resolveOzonPriceAccountChoices(
                    selectedItems,
                    this.selected,
                );
                var freshChoice = latest.choices.find(function (choice) {
                    return choice.account_id === accountId;
                });
                var reviewedChoice = review.choices.find(function (choice) {
                    return choice.account_id === accountId;
                });
                if (!freshChoice || !reviewedChoice ||
                    !sameIds(freshChoice.listing_ids, reviewedChoice.listing_ids)) {
                    this.closeOzonPriceHandoff();
                    this.setOzonPriceHandoffError(
                        'Связанные карточки изменились после проверки. Обновите список и повторите действие.'
                    );
                    return false;
                }
                try {
                    if (typeof this.urls.ozonCommercial !== 'string' ||
                        !this.urls.ozonCommercial) throw new Error('missing URL');
                    var target = new URL(this.urls.ozonCommercial, window.location.origin);
                    if (target.origin !== window.location.origin) throw new Error('foreign URL');
                    target.searchParams.set('account_id', String(accountId));
                    target.searchParams.set('bulk_listing_ids', freshChoice.listing_ids.join(','));
                    window.location.assign(target.pathname + target.search);
                    return true;
                } catch (_) {
                    this.closeOzonPriceHandoff();
                    this.setOzonPriceHandoffError(
                        'Не удалось открыть сравнение цен Ozon. Повторите действие со свежим выбором.'
                    );
                    return false;
                }
            },

            /* ---------- массовые действия ---------- */
            startAction: function (action) {
                if (this.loading || this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) {
                    this.error = 'Список обновляется. Дождитесь загрузки и выберите товары снова.';
                    return;
                }
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
                    if (!selectionIds.length || selectionIds.length > MAX_BULK_SELECTION) {
                        this.ozonError = 'Выберите от 1 до 200 товаров для одной подготовки.';
                        return;
                    }
                    this.openOzonPreparation(selectionIds);
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
                if (action.key === 'ozon_prices') {
                    if (selectionIds.length > MAX_OZON_PRICE_HANDOFF) {
                        this.setOzonPriceHandoffError(
                            'За одно сравнение Ozon можно выбрать не больше 100 товаров.'
                        );
                        return;
                    }
                    this.openOzonPriceHandoff(chosen, selectionIds);
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
                    labels: selectedProductLabels(chosen, effectIds),
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
            prepareRefreshedForOzon: function (event) {
                var result = this.supplierRefreshResult;
                if (!this.ozonEnabled || !result ||
                    !['complete', 'partial'].includes(result.kind) ||
                    !this.supplierRefreshOzonIds.length) return;

                return this.openOzonPreparation(
                    this.supplierRefreshOzonIds,
                    event && event.currentTarget,
                );
            },
            openOzonPreparation: function (productIds, returnFocus) {
                if (!this.ozonEnabled || this.ozonPreparationDisabled) return false;
                if (!Array.isArray(productIds) || !productIds.length ||
                    productIds.length > MAX_BULK_SELECTION ||
                    productIds.some(function (id) {
                        return !Number.isSafeInteger(id) || id <= 0;
                    }) || new Set(productIds).size !== productIds.length) {
                    this.ozonError = 'Не удалось проверить список товаров. Обновите его и повторите действие.';
                    this.$nextTick(function () {
                        this.$refs.ozonError?.focus();
                    });
                    return false;
                }
                this.ozonIds = productIds.slice();
                this.ozonAccountId = this.ozonAccounts.length === 1
                    ? String(this.ozonAccounts[0].id) : '';
                this.ozonConfirmed = false;
                this.ozonError = '';
                this.ozonReturnFocus = returnFocus || null;
                this.$nextTick(function () {
                    this.$refs.ozonDialog.showModal();
                    this.$refs.ozonAccount.focus();
                });
                return true;
            },
            closeOzonDialog: function () {
                if (this.ozonBusy) return;
                this.$refs.ozonDialog.close();
                this.ozonConfirmed = false;
                if (this.ozonReturnFocus && this.ozonReturnFocus.isConnected &&
                    typeof this.ozonReturnFocus.focus === 'function') {
                    this.ozonReturnFocus.focus();
                }
                this.ozonReturnFocus = null;
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
    });
})();
