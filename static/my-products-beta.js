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
                confirm: {
                    open: false, key: null, title: '', message: '',
                    details: [], label: 'Продолжить', danger: false, busy: false,
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
            /* Действия зависят от того, ЧТО выбрано: публиковать нечего, если
               все карточки уже на WB, и наоборот. */
            actionGroups: function () {
                var chosen = this.selectedItems;
                var published = chosen.filter(function (i) { return i.wb; }).length;
                var unpublished = chosen.length - published;
                var withUpdates = chosen.filter(function (i) { return i.has_supplier_update; }).length;
                var groups = [];

                groups.push({
                    title: 'Публикация',
                    items: [
                        {
                            key: 'push_wb',
                            label: 'Отправить на Wildberries',
                            hint: unpublished
                                ? 'создаст ' + unpublished + ' '
                                    + this.$fmt.plural(unpublished, 'новую карточку', 'новые карточки', 'новых карточек')
                                : 'все выбранные уже на WB',
                            disabled: !this.wbConnected || !unpublished,
                        },
                        this.ozonEnabled ? {
                            key: 'ozon',
                            label: 'Подготовить для Ozon',
                            hint: 'создаст локальные черновики; отправка подтверждается отдельно',
                            disabled: !this.ozonAccounts.length || !!this.ozonPending || this.ozonStorageProblem || !!this.ozonBusy,
                        } : null,
                    ].filter(Boolean),
                });

                groups.push({
                    title: 'Данные и контент',
                    items: [
                        {
                            key: 'refresh_supplier',
                            label: 'Подтянуть данные поставщика',
                            hint: withUpdates
                                ? 'у ' + withUpdates + ' ' + this.$fmt.plural(withUpdates, 'карточки', 'карточек', 'карточек') + ' есть новые данные'
                                : 'обновит названия, характеристики, фото',
                        },
                        {
                            key: 'enrich',
                            label: 'Дополнить карточки на WB',
                            hint: published
                                ? 'фото и характеристики для ' + published + ' '
                                    + this.$fmt.plural(published, 'карточки', 'карточек', 'карточек')
                                : 'только для опубликованных',
                            disabled: !published,
                        },
                        {
                            key: 'audit',
                            label: 'Сверить с Wildberries',
                            hint: 'покажет расхождения фото и характеристик',
                            disabled: !this.wbConnected || !published,
                        },
                    ],
                });

                groups.push({
                    title: 'Опасные',
                    danger: true,
                    items: [{
                        key: 'delete',
                        label: 'Удалить из «Моих товаров»',
                        hint: published
                            ? published + ' ' + this.$fmt.plural(published, 'карточка уже на маркетплейсе — останется', 'карточки уже на маркетплейсе — останутся', 'карточек уже на маркетплейсе — останутся') + ' там'
                            : 'станут доступны для повторного импорта',
                        danger: true,
                    }],
                });
                return groups;
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
                var published = chosen.filter(function (i) { return i.wb; }).length;
                var plans = {
                    push_wb: {
                        title: 'Отправить на Wildberries',
                        message: 'Создадим карточки для ' + (chosen.length - published) + ' '
                            + this.$fmt.plural(chosen.length - published, 'товара', 'товаров', 'товаров')
                            + ', которых там ещё нет.',
                        details: [
                            'Уже опубликованные карточки пропустим',
                            'Цены и остатки не меняем — только создаём карточки',
                            'Отправка идёт в фоне, за ней можно следить здесь',
                        ],
                        label: 'Отправить',
                    },
                    refresh_supplier: {
                        title: 'Подтянуть данные поставщика',
                        message: 'Обновим ' + chosen.length + ' '
                            + this.$fmt.plural(chosen.length, 'карточку', 'карточки', 'карточек')
                            + ' данными из каталога поставщика.',
                        details: [
                            'Меняются только ваши карточки в Seller Hub',
                            'Карточки на маркетплейсах не изменятся',
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
                    // Экран подтверждения обогащения — отдельный, с выбором полей
                    window.location.href = this.urls.enrichBulk + '?ids='
                        + this.selected.join(',');
                    return;
                }
                if (action.key === 'ozon') {
                    if (this.ozonPending || this.ozonStorageProblem || this.ozonBusy || this.ozonSessionEnded) return;
                    if (!chosen.length || chosen.length > 200) {
                        this.ozonError = 'Выберите от 1 до 200 товаров для одной подготовки.';
                        return;
                    }
                    this.ozonIds = chosen.map(function (item) { return item.id; });
                    this.ozonAccountId = this.ozonAccounts.length === 1 ? String(this.ozonAccounts[0].id) : '';
                    this.ozonConfirmed = false;
                    this.ozonError = '';
                    this.$nextTick(function () {
                        this.$refs.ozonDialog.showModal();
                        this.$refs.ozonAccount.focus();
                    });
                    return;
                }
                var plan = plans[action.key];
                if (!plan) return;
                this.confirm = {
                    open: true,
                    key: action.key,
                    title: plan.title,
                    message: plan.message,
                    details: plan.details,
                    label: plan.label,
                    danger: !!plan.danger,
                    busy: false,
                };
            },
            runAction: function () {
                var self = this;
                var key = this.confirm.key;
                var ids = this.selected.slice();
                if (!key || !ids.length) return;
                this.confirm.busy = true;

                var request;
                if (key === 'push_wb') {
                    this.jobTitle = 'Отправляем на Wildberries';
                    request = this.$api.post(this.urls.pushToWb, { product_ids: ids });
                } else if (key === 'refresh_supplier') {
                    this.jobTitle = 'Обновляем из каталога поставщика';
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
                    self.selected = [];
                    if (data.job_uid || data.job_id) {
                        self.trackJob(data.job_uid || data.job_id);
                    } else {
                        // Синхронное действие: показываем итог сразу
                        self.job = {
                            status: 'done',
                            total: ids.length,
                            processed: ids.length,
                            succeeded: data.updated || data.deleted || ids.length,
                            failed: data.errors || 0,
                            message: data.message || '',
                        };
                        self.load();
                        self.loadFacets();
                    }
                }).catch(function (err) {
                    self.error = err.message || 'Действие не выполнено';
                    self.confirm.open = false;
                }).finally(function () {
                    self.confirm.busy = false;
                });
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
            trackJob: function (jobUid) {
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
                this.job = { status: 'running', total: this.selected.length, processed: 0 };
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
