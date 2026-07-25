/* global Chart */

function competitorGroupDetail(config) {
    return {
        groupId: Number(config.groupId),
        products: Array.isArray(config.products) ? config.products : [],
        importRequested: Boolean(config.importRequested),
        sourceSupplierId: config.sourceSupplierId ? Number(config.sourceSupplierId) : null,

        compare: null,
        compareLoading: true,
        compareError: null,
        syncing: false,

        matchData: null,
        matchesLoading: true,
        matchesError: null,
        matching: false,
        matchJob: null,
        matchFilter: 'all',
        reviewItem: null,
        reviewSupplier: null,
        reviewSaving: false,
        supplierSearchQuery: '',
        supplierSearchResults: null,
        supplierSearching: false,
        supplierSearchError: null,
        _matchPollTimer: null,
        _supplierSearchController: null,

        productQuery: '',
        sellerFilter: 'all',
        productSort: 'price_asc',

        showAddModal: false,
        addMode: 'seller',
        adding: false,
        nmInput: '',

        searchQuery: '',
        searching: false,
        searchResults: null,
        searchError: null,
        searchSelectedIds: [],
        _searchController: null,

        supplierIdInput: '',
        sellerResolvedId: null,
        sellerLoading: false,
        sellerPreview: null,
        sellerPreviewSource: null,
        sellerError: null,
        sellerRateLimited: false,
        sellerProductQuery: '',
        sellerSelectedIds: [],
        _sellerController: null,
        queuedResumeRunning: false,

        pendingRemoval: null,
        removing: false,

        historyProduct: null,
        historyPeriod: '30d',
        historyLoading: false,
        historyError: null,
        historyHasData: false,
        _chart: null,

        init() {
            this.loadCompare();
            this.loadMatches();
            this.$watch('historyPeriod', () => {
                if (this.historyProduct) this.loadHistory();
            });

            if (this.sourceSupplierId) {
                this.supplierIdInput = String(this.sourceSupplierId);
            }
            const url = new URL(window.location.href);
            const requestedMode = url.searchParams.get('add');
            const shouldResumeImport = Boolean(
                this.importRequested && this.sourceSupplierId);
            if (shouldResumeImport) this.queuedResumeRunning = true;
            if (['seller', 'search', 'nm_ids'].includes(requestedMode)) {
                this.openAdd(requestedMode);
                url.searchParams.delete('add');
                window.history.replaceState({}, '', url.pathname + url.search + url.hash);
            }
            if (shouldResumeImport) {
                this.$nextTick(() => this.resumeQueuedSellerImport());
            }
        },

        csrf() {
            return document.querySelector('meta[name="csrf-token"]')?.content || '';
        },

        parseUTC(value) {
            if (!value) return null;
            const iso = /[zZ]$|[+-]\d\d:?\d\d$/.test(value) ? value : value + 'Z';
            const date = new Date(iso);
            return Number.isNaN(date.getTime()) ? null : date;
        },

        fmtPrice(value) {
            if (value === null || value === undefined || value === '') return '—';
            return new Intl.NumberFormat('ru-RU').format(Math.round(Number(value))) + ' ₽';
        },

        fmtInteger(value) {
            if (value === null || value === undefined || value === '') return '—';
            return new Intl.NumberFormat('ru-RU').format(Number(value));
        },

        shortDate(date) {
            return date
                ? date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })
                : '—';
        },

        shortDateTime(date) {
            return date
                ? date.toLocaleString('ru-RU', {
                    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'
                })
                : '—';
        },

        plural(number, forms) {
            const n = Math.abs(Number(number));
            const mod10 = n % 10;
            const mod100 = n % 100;
            if (mod10 === 1 && mod100 !== 11) return forms[0];
            if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
            return forms[2];
        },

        priceTs(product) {
            return this.parseUTC(product.last_price_at || product.last_fetched_at);
        },

        isStale(product) {
            const timestamp = this.priceTs(product);
            return Boolean(timestamp) && (Date.now() - timestamp.getTime() > 24 * 60 * 60 * 1000);
        },

        discountTone(product) {
            const discount = Number(product.current_discount_percent || 0);
            if (discount >= 30) return 'sh-badge--red';
            if (discount >= 15) return 'sh-badge--yellow';
            return 'sh-badge--green';
        },

        sellerKey(product) {
            return product.wb_supplier_id ? String(product.wb_supplier_id) : 'unknown';
        },

        sellerOptions() {
            const sellers = new Map();
            for (const product of this.products) {
                const key = this.sellerKey(product);
                const current = sellers.get(key) || {
                    key,
                    label: product.supplier_name || (key === 'unknown' ? 'Продавец уточняется' : 'Продавец #' + key),
                    count: 0,
                };
                current.count += 1;
                if (!current.label && product.supplier_name) current.label = product.supplier_name;
                sellers.set(key, current);
            }
            return Array.from(sellers.values()).sort((a, b) =>
                a.label.localeCompare(b.label, 'ru'));
        },

        sellersCount() {
            return this.sellerOptions().filter(item => item.key !== 'unknown').length;
        },

        visibleProducts() {
            const query = this.productQuery.trim().toLocaleLowerCase('ru');
            const rows = this.products.filter(product => {
                if (this.sellerFilter !== 'all' && this.sellerKey(product) !== this.sellerFilter) {
                    return false;
                }
                if (!query) return true;
                const haystack = [
                    product.title,
                    product.brand,
                    product.supplier_name,
                    product.nm_id,
                    product.wb_supplier_id,
                ].filter(value => value !== null && value !== undefined)
                    .join(' ')
                    .toLocaleLowerCase('ru');
                return haystack.includes(query);
            });

            const price = product => {
                const value = product.current_sale_price ?? product.current_price;
                return value === null || value === undefined ? null : Number(value);
            };
            const compareNullable = (left, right, direction) => {
                if (left === null && right === null) return 0;
                if (left === null) return 1;
                if (right === null) return -1;
                return (left - right) * direction;
            };

            rows.sort((a, b) => {
                if (this.productSort === 'price_desc') {
                    return compareNullable(price(a), price(b), -1);
                }
                if (this.productSort === 'rating_desc') {
                    return compareNullable(
                        a.current_rating === null || a.current_rating === undefined ? null : Number(a.current_rating),
                        b.current_rating === null || b.current_rating === undefined ? null : Number(b.current_rating),
                        -1
                    );
                }
                if (this.productSort === 'newest') {
                    return Number(b.id || 0) - Number(a.id || 0);
                }
                return compareNullable(price(a), price(b), 1);
            });
            return rows;
        },

        ownPrice() {
            const own = this.compare && this.compare.own_product;
            return own ? (own.discount_price ?? own.price ?? null) : null;
        },

        async loadCompare() {
            this.compareLoading = true;
            this.compareError = null;
            try {
                const response = await fetch(`/api/competitors/compare/${this.groupId}`);
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || 'Ошибка загрузки');
                this.compare = data;
            } catch (error) {
                this.compareError = error.message;
            } finally {
                this.compareLoading = false;
            }
        },

        async loadMatches(silent = false) {
            if (!silent) this.matchesLoading = true;
            this.matchesError = null;
            try {
                const response = await fetch(
                    `/api/competitors/groups/${this.groupId}/matches`);
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || 'Ошибка загрузки сопоставлений');
                this.matchData = data;
                if (data.active_job) {
                    this.matchJob = data.active_job;
                    this.matching = true;
                    this.scheduleMatchPoll();
                } else if (this.matchJob?.status === 'completed') {
                    this.matching = false;
                }
            } catch (error) {
                this.matchesError = error.message || 'Не удалось загрузить сопоставления';
            } finally {
                this.matchesLoading = false;
            }
        },

        matchItems() {
            const items = Array.isArray(this.matchData?.items) ? this.matchData.items : [];
            if (this.matchFilter === 'matched') {
                return items.filter(item => item.effective_supplier);
            }
            if (this.matchFilter === 'review') {
                return items.filter(item => !item.effective_supplier ||
                    !item.review || item.match?.score < 72);
            }
            return items;
        },

        matchedCount() {
            return (this.matchData?.items || []).filter(item => item.effective_supplier).length;
        },

        calculatedCount() {
            return (this.matchData?.items || []).filter(item =>
                item.match?.status === 'completed').length;
        },

        reviewedCount() {
            return (this.matchData?.items || []).filter(item => item.review).length;
        },

        matchProgress() {
            const job = this.matchJob;
            if (!job || !job.total) return 0;
            return Math.max(0, Math.min(100,
                Math.round(Number(job.processed || 0) / Number(job.total) * 100)));
        },

        confidenceTone(item) {
            const score = Number(item?.match?.score || 0);
            if (item?.match?.match_type === 'different') return 'is-low';
            if (score >= 82) return 'is-high';
            if (score >= 58) return 'is-medium';
            return 'is-low';
        },

        priceGapText(item) {
            const gap = item?.own_product?.price_gap_percent;
            if (gap === null || gap === undefined) return null;
            if (Number(gap) === 0) return 'цены равны';
            return (Number(gap) > 0 ? '+' : '') +
                Number(gap).toLocaleString('ru-RU') + '% к конкуренту';
        },

        async runMatching() {
            if (this.matching || !this.products.length) return;
            this.matching = true;
            this.matchesError = null;
            try {
                const response = await fetch(
                    `/api/competitors/groups/${this.groupId}/matches/run`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': this.csrf(),
                        },
                        body: JSON.stringify({}),
                    });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || 'Не удалось запустить сопоставление');
                this.matchJob = data.job;
                this.$store.toasts.info(
                    'Сопоставление запущено. Готовые общие результаты будут переиспользованы.');
                this.scheduleMatchPoll(1200);
            } catch (error) {
                this.matching = false;
                this.$store.toasts.error(error.message || 'Не удалось запустить сопоставление');
            }
        },

        scheduleMatchPoll(delay = 3500) {
            if (this._matchPollTimer) window.clearTimeout(this._matchPollTimer);
            if (!this.matchJob?.job_uid) return;
            this._matchPollTimer = window.setTimeout(() => this.pollMatchJob(), delay);
        },

        async pollMatchJob() {
            if (!this.matchJob?.job_uid) return;
            try {
                const response = await fetch(
                    `/api/competitors/matches/jobs/${encodeURIComponent(this.matchJob.job_uid)}`);
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || 'Статус задачи недоступен');
                this.matchJob = data;
                if (['completed', 'failed'].includes(data.status)) {
                    this.matching = false;
                    await this.loadMatches();
                    if (data.status === 'completed') {
                        const cacheHits = Number(data.result?.cache_hits || 0);
                        this.$store.toasts.success(
                            `Сопоставление готово${cacheHits ? ` · из общего кэша: ${cacheHits}` : ''}`);
                    } else {
                        this.$store.toasts.error('Задача сопоставления завершилась с ошибкой');
                    }
                    return;
                }
                this.matching = true;
                this.scheduleMatchPoll();
            } catch (error) {
                this.matchesError = error.message;
                this.scheduleMatchPoll(7000);
            }
        },

        openMatchReview(item) {
            if (!item?.match) return;
            this.reviewItem = item;
            const selectedId = item.review?.supplier_product_id ||
                item.effective_supplier?.id ||
                item.match.candidates?.[0]?.supplier?.id;
            const candidate = (item.match.candidates || []).find(row =>
                Number(row.supplier?.id) === Number(selectedId));
            this.reviewSupplier = candidate?.supplier || item.effective_supplier || null;
            this.supplierSearchQuery = '';
            this.supplierSearchResults = null;
            this.supplierSearchError = null;
        },

        closeMatchReview() {
            if (this.reviewSaving) return;
            if (this._supplierSearchController) this._supplierSearchController.abort();
            this.reviewItem = null;
            this.reviewSupplier = null;
            this.supplierSearchResults = null;
            this.supplierSearchError = null;
        },

        chooseReviewSupplier(supplier) {
            this.reviewSupplier = supplier || null;
        },

        async searchSupplierCatalog() {
            const query = this.supplierSearchQuery.trim();
            if (query.length < 2 || this.supplierSearching) return;
            if (this._supplierSearchController) this._supplierSearchController.abort();
            const controller = new AbortController();
            this._supplierSearchController = controller;
            this.supplierSearching = true;
            this.supplierSearchError = null;
            try {
                const response = await fetch(
                    '/api/competitors/supplier-products/search?q=' + encodeURIComponent(query),
                    { signal: controller.signal });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || 'Поиск не удался');
                this.supplierSearchResults = Array.isArray(data.items) ? data.items : [];
            } catch (error) {
                if (error.name === 'AbortError') return;
                this.supplierSearchError = error.message || 'Поиск не удался';
            } finally {
                if (this._supplierSearchController === controller) {
                    this.supplierSearching = false;
                    this._supplierSearchController = null;
                }
            }
        },

        async saveMatchReview(action, matchType = null) {
            if (!this.reviewItem?.match || this.reviewSaving) return;
            if (action === 'confirm' && !this.reviewSupplier) {
                this.$store.toasts.error('Выберите товар поставщика');
                return;
            }
            this.reviewSaving = true;
            try {
                const response = await fetch(
                    `/api/competitors/matches/${this.reviewItem.match.id}/review`, {
                        method: 'PUT',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': this.csrf(),
                        },
                        body: JSON.stringify({
                            action,
                            supplier_product_id: action === 'confirm'
                                ? Number(this.reviewSupplier.id) : null,
                            match_type: action === 'confirm' ? matchType : null,
                        }),
                    });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || 'Не удалось сохранить решение');
                this.$store.toasts.success(
                    action === 'confirm' ? 'Соответствие сохранено для вашего магазина' :
                        (action === 'reject' ? 'Предложение отклонено для вашего магазина' :
                            'Вернулись к общей рекомендации'));
                this.reviewItem = null;
                this.reviewSupplier = null;
                await this.loadMatches();
            } catch (error) {
                this.$store.toasts.error(error.message || 'Не удалось сохранить решение');
            } finally {
                this.reviewSaving = false;
            }
        },

        async forceSync() {
            if (this.syncing) return;
            this.syncing = true;
            try {
                const response = await fetch('/api/competitors/sync', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': this.csrf() },
                    body: JSON.stringify({}),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(data.error || 'Не удалось запланировать синхронизацию');
                }
                this.$store.toasts.info(
                    data.message || 'Синхронизация запустится в течение минуты');
            } catch (error) {
                this.$store.toasts.error(error.message || 'Сеть недоступна — попробуйте ещё раз');
            } finally {
                this.syncing = false;
            }
        },

        openAdd(mode = 'seller') {
            this.addMode = ['seller', 'search', 'nm_ids'].includes(mode) ? mode : 'seller';
            this.showAddModal = true;
            this.$nextTick(() => {
                const refName = this.addMode === 'seller'
                    ? 'sellerInput'
                    : (this.addMode === 'search' ? 'searchInput' : 'nmInput');
                this.$refs[refName]?.focus();
                if (this.addMode === 'seller' && this.sourceSupplierId &&
                        !this.sellerPreview && !this.sellerLoading &&
                        !this.queuedResumeRunning) {
                    this.loadSellerPreview();
                }
            });
        },

        closeAdd() {
            if (this.adding) return;
            this.showAddModal = false;
            if (this._sellerController) this._sellerController.abort();
            if (this._searchController) this._searchController.abort();
            this.sellerLoading = false;
            this.searching = false;
        },

        parseSupplierId(rawValue = this.supplierIdInput) {
            const raw = String(rawValue || '').trim();
            if (/^\d+$/.test(raw)) {
                const direct = Number(raw);
                return Number.isSafeInteger(direct) && direct > 0 ? direct : null;
            }
            const pathMatch = raw.match(/\/seller\/(\d+)/i);
            if (pathMatch) {
                const fromPath = Number(pathMatch[1]);
                return Number.isSafeInteger(fromPath) && fromPath > 0 ? fromPath : null;
            }
            try {
                const url = new URL(raw);
                for (const key of ['supplier', 'supplier_id', 'seller', 'seller_id']) {
                    const value = url.searchParams.get(key);
                    if (value && /^\d+$/.test(value)) {
                        const fromQuery = Number(value);
                        if (Number.isSafeInteger(fromQuery) && fromQuery > 0) return fromQuery;
                    }
                }
            } catch (error) {
                // Пользователь мог вставить не URL — ниже вернётся null.
            }
            return null;
        },

        sellerInputChanged() {
            const parsed = this.parseSupplierId();
            if (parsed !== this.sellerResolvedId) {
                this.sellerPreview = null;
                this.sellerPreviewSource = null;
                this.sellerResolvedId = null;
                this.sellerSelectedIds = [];
                this.sellerProductQuery = '';
                this.sellerError = null;
                this.sellerRateLimited = false;
            }
        },

        wbSellerCatalogUrl(supplierId, page = 1) {
            const url = new URL('https://catalog.wb.ru/sellers/v4/catalog');
            const params = {
                ab_testing: 'false',
                appType: '1',
                curr: 'rub',
                dest: '-1257786',
                hide_dtype: '13',
                lang: 'ru',
                page: String(page),
                sort: 'popular',
                spp: '30',
                supplier: String(supplierId),
            };
            for (const [key, value] of Object.entries(params)) {
                url.searchParams.set(key, value);
            }
            return url.toString();
        },

        parseBrowserCatalogProduct(raw) {
            if (!raw || typeof raw !== 'object') return null;
            const nmId = Number(raw.id);
            if (!Number.isSafeInteger(nmId) || nmId <= 0) return null;
            const sizes = Array.isArray(raw.sizes) ? raw.sizes : [];
            const price = sizes[0]?.price || {};
            const toRubles = value => {
                const amount = Number(value);
                return Number.isFinite(amount) && amount > 0
                    ? Math.floor(amount / 100)
                    : null;
            };
            let totalStock = Number(raw.totalQuantity);
            if (!Number.isFinite(totalStock) || totalStock < 0) totalStock = 0;
            return {
                nm_id: nmId,
                title: typeof raw.name === 'string' ? raw.name : '',
                brand: typeof raw.brand === 'string' ? raw.brand : '',
                supplier_name: typeof raw.supplier === 'string' ? raw.supplier : '',
                wb_supplier_id: Number(raw.supplierId) || null,
                image_url: null,
                price: toRubles(price.basic ?? raw.priceU),
                sale_price: toRubles(
                    price.total ?? price.product ?? raw.salePriceU,
                ),
                rating: Number(raw.reviewRating) || null,
                feedbacks_count: Number(raw.feedbacks) || 0,
                total_stock: totalStock,
            };
        },

        async fetchSellerCatalogFromBrowser(supplierId, page = 1, signal = undefined) {
            const response = await fetch(this.wbSellerCatalogUrl(supplierId, page), {
                method: 'GET',
                mode: 'cors',
                credentials: 'omit',
                signal,
            });
            if (!response.ok) {
                const error = new Error('WB ограничил прямую загрузку каталога');
                error.status = response.status;
                throw error;
            }
            const data = await response.json();
            const rawProducts = Array.isArray(data?.products)
                ? data.products
                : (Array.isArray(data?.data?.products) ? data.data.products : []);
            return rawProducts.slice(0, 100)
                .map(item => this.parseBrowserCatalogProduct(item))
                .filter(Boolean);
        },

        async loadSellerPreview() {
            const supplierId = this.parseSupplierId();
            if (!supplierId) {
                this.sellerError = 'Вставьте числовой ID или ссылку вида wildberries.ru/seller/123456';
                return;
            }
            if (this._sellerController) this._sellerController.abort();
            const controller = new AbortController();
            this._sellerController = controller;
            this.sellerLoading = true;
            this.sellerError = null;
            this.sellerRateLimited = false;
            this.sellerPreview = null;
            this.sellerPreviewSource = null;
            this.sellerSelectedIds = [];
            this.sellerProductQuery = '';
            // ID уже строго разобран локально: даже если WB ограничит preview,
            // durable import можно безопасно поставить в очередь без provider
            // call внутри mutating POST.
            this.sellerResolvedId = supplierId;
            this.supplierIdInput = String(supplierId);
            try {
                const response = await fetch(
                    '/api/competitors/seller-catalog?supplier_id=' + supplierId + '&page=1',
                    { signal: controller.signal }
                );
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    const rateLimited = response.status === 503 &&
                        data.code === 'wb_rate_limited';
                    if (rateLimited) {
                        try {
                            const directRows = await this.fetchSellerCatalogFromBrowser(
                                supplierId, 1, controller.signal);
                            if (this._sellerController !== controller) return;
                            this.sellerPreview = directRows;
                            this.sellerPreviewSource = 'browser';
                            this.sellerRateLimited = false;
                            return;
                        } catch (directError) {
                            if (directError.name === 'AbortError') throw directError;
                            this.sellerRateLimited = true;
                        }
                    }
                    throw new Error(data.error || 'Не удалось загрузить каталог продавца');
                }
                if (this._sellerController !== controller) return;
                this.sellerPreview = Array.isArray(data) ? data : [];
                this.sellerPreviewSource = 'server';
            } catch (error) {
                if (error.name === 'AbortError') return;
                this.sellerError = error.message || 'Не удалось загрузить каталог продавца';
            } finally {
                if (this._sellerController === controller) {
                    this.sellerLoading = false;
                    this._sellerController = null;
                }
            }
        },

        sellerName() {
            const item = (this.sellerPreview || []).find(product => product.supplier_name);
            return item?.supplier_name || ('Продавец #' + this.sellerResolvedId);
        },

        sellerInitial() {
            const name = this.sellerName().trim();
            return name ? name.slice(0, 1).toLocaleUpperCase('ru') : 'W';
        },

        sellerMinPrice() {
            const prices = (this.sellerPreview || [])
                .map(item => item.sale_price ?? item.price)
                .filter(value => value !== null && value !== undefined)
                .map(Number);
            return prices.length ? Math.min(...prices) : null;
        },

        filteredSellerPreview() {
            const query = this.sellerProductQuery.trim().toLocaleLowerCase('ru');
            if (!query) return this.sellerPreview || [];
            return (this.sellerPreview || []).filter(item => [
                item.title, item.brand, item.nm_id
            ].filter(Boolean).join(' ').toLocaleLowerCase('ru').includes(query));
        },

        isTracked(nmId) {
            const id = Number(nmId);
            return this.products.some(product => Number(product.nm_id) === id && product.is_active !== false);
        },

        selectedIncludes(collection, nmId) {
            const id = Number(nmId);
            return collection.map(Number).includes(id);
        },

        allSellerVisibleSelected() {
            const available = this.filteredSellerPreview().filter(item => !this.isTracked(item.nm_id));
            return available.length > 0 && available.every(item =>
                this.selectedIncludes(this.sellerSelectedIds, item.nm_id));
        },

        toggleSellerVisible() {
            const visibleIds = this.filteredSellerPreview()
                .filter(item => !this.isTracked(item.nm_id))
                .map(item => Number(item.nm_id));
            if (!visibleIds.length) return;
            const selected = new Set(this.sellerSelectedIds.map(Number));
            if (visibleIds.every(id => selected.has(id))) {
                visibleIds.forEach(id => selected.delete(id));
            } else {
                visibleIds.forEach(id => selected.add(id));
            }
            this.sellerSelectedIds = Array.from(selected);
        },

        selectedSellerItems() {
            const selected = new Set(this.sellerSelectedIds.map(Number));
            return (this.sellerPreview || []).filter(item => selected.has(Number(item.nm_id)));
        },

        async runSearch() {
            const query = this.searchQuery.trim();
            if (!query || this.searching) return;
            if (this._searchController) this._searchController.abort();
            const controller = new AbortController();
            this._searchController = controller;
            this.searching = true;
            this.searchError = null;
            this.searchResults = null;
            this.searchSelectedIds = [];
            try {
                const response = await fetch(
                    '/api/competitors/search?q=' + encodeURIComponent(query),
                    { signal: controller.signal }
                );
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || 'Не удалось выполнить поиск');
                if (this._searchController !== controller) return;
                this.searchResults = Array.isArray(data) ? data : [];
            } catch (error) {
                if (error.name === 'AbortError') return;
                this.searchError = error.message || 'Не удалось выполнить поиск';
            } finally {
                if (this._searchController === controller) {
                    this.searching = false;
                    this._searchController = null;
                }
            }
        },

        allSearchSelected() {
            const available = (this.searchResults || []).filter(item => !this.isTracked(item.nm_id));
            return available.length > 0 && available.every(item =>
                this.selectedIncludes(this.searchSelectedIds, item.nm_id));
        },

        toggleSearchAll() {
            const ids = (this.searchResults || [])
                .filter(item => !this.isTracked(item.nm_id))
                .map(item => Number(item.nm_id));
            if (!ids.length) return;
            const selected = new Set(this.searchSelectedIds.map(Number));
            if (ids.every(id => selected.has(id))) {
                ids.forEach(id => selected.delete(id));
            } else {
                ids.forEach(id => selected.add(id));
            }
            this.searchSelectedIds = Array.from(selected);
        },

        selectedSearchItems() {
            const selected = new Set(this.searchSelectedIds.map(Number));
            return (this.searchResults || []).filter(item => selected.has(Number(item.nm_id)));
        },

        parsedNm() {
            const tokens = this.nmInput.split(/[\s,;]+/).filter(Boolean);
            const seen = new Set();
            const valid = [];
            let invalid = 0;
            let duplicates = 0;
            for (const token of tokens) {
                if (!/^\d+$/.test(token)) {
                    invalid += 1;
                    continue;
                }
                const value = Number(token);
                if (!Number.isSafeInteger(value) || value <= 0) {
                    invalid += 1;
                } else if (seen.has(value)) {
                    duplicates += 1;
                } else {
                    seen.add(value);
                    valid.push(value);
                }
            }
            return { valid, invalid, duplicates };
        },

        async postProducts(body) {
            const response = await fetch('/api/competitors/products', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': this.csrf() },
                body: JSON.stringify(body),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || 'Не удалось добавить товары');
            return data;
        },

        mergeAddedProducts(rows, previewItems = []) {
            if (!Array.isArray(rows)) return;
            const previewById = new Map(previewItems.map(item => [Number(item.nm_id), item]));
            const byNmId = new Map(this.products.map(item => [Number(item.nm_id), item]));
            for (const row of rows) {
                const nmId = Number(row.nm_id);
                const preview = previewById.get(nmId) || {};
                const merged = {
                    ...row,
                    title: row.title || preview.title || null,
                    brand: row.brand || preview.brand || null,
                    supplier_name: row.supplier_name || preview.supplier_name || null,
                    wb_supplier_id: row.wb_supplier_id || preview.wb_supplier_id || null,
                    image_url: row.image_url || preview.image_url || null,
                    _preview_metadata: !row.title && Boolean(preview.title),
                };
                const existing = byNmId.get(nmId);
                if (existing) Object.assign(existing, merged);
                else {
                    this.products.unshift(merged);
                    byNmId.set(nmId, merged);
                }
            }
            this.products = [...this.products];
        },

        addResultMessage(data) {
            const parts = [];
            if (data.added) parts.push('добавлено ' + data.added);
            if (data.reactivated) parts.push('возвращено ' + data.reactivated);
            if (data.skipped) parts.push('уже отслеживалось ' + data.skipped);
            return parts.length ? parts.join(', ') : 'Список не изменился';
        },

        async addExact(nmIds, previewItems = []) {
            const ids = Array.from(new Set(nmIds.map(Number).filter(id =>
                Number.isSafeInteger(id) && id > 0)));
            if (!ids.length || ids.length > 300 || this.adding) return;
            this.adding = true;
            try {
                const data = await this.postProducts({ group_id: this.groupId, nm_ids: ids });
                this.mergeAddedProducts(data.products, previewItems);
                this.$store.toasts.success(
                    this.addResultMessage(data) + '. Цены подтянутся в течение минуты',
                    'Мониторинг обновлён'
                );
                this.searchSelectedIds = [];
                this.sellerSelectedIds = [];
                this.nmInput = '';
                this.showAddModal = false;
                this.loadCompare();
            } catch (error) {
                this.$store.toasts.error(error.message || 'Не удалось добавить товары');
            } finally {
                this.adding = false;
            }
        },

        addByNmIds() {
            const parsed = this.parsedNm();
            if (!parsed.valid.length || parsed.valid.length > 300) return;
            return this.addExact(parsed.valid);
        },

        addSearchSelected() {
            return this.addExact(this.searchSelectedIds, this.selectedSearchItems());
        },

        addSellerSelected() {
            return this.addExact(this.sellerSelectedIds, this.selectedSellerItems());
        },

        canQueueSellerCatalog() {
            return Boolean(this.sellerResolvedId || this.parseSupplierId());
        },

        async resumeQueuedSellerImport() {
            try {
                await this.loadSellerPreview();
                if (this.importRequested && Array.isArray(this.sellerPreview) &&
                        this.sellerPreview.length) {
                    await this.importSellerCatalog();
                }
            } finally {
                this.queuedResumeRunning = false;
            }
        },

        async clearSellerImportRequest() {
            const response = await fetch(
                `/api/competitors/groups/${this.groupId}/import`,
                { method: 'DELETE', headers: { 'X-CSRFToken': this.csrf() } }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.error || 'Не удалось остановить очередь импорта');
            }
            this.importRequested = false;
            return data;
        },

        async cancelSellerImport() {
            if (!this.importRequested || this.adding) return;
            this.adding = true;
            try {
                await this.clearSellerImportRequest();
                this.$store.toasts.info(
                    'Уже добавленные товары останутся под мониторингом',
                    'Очередь импорта остановлена'
                );
            } catch (error) {
                this.$store.toasts.error(error.message || 'Не удалось остановить импорт');
            } finally {
                this.adding = false;
            }
        },

        async importSellerCatalog() {
            const supplierId = this.sellerResolvedId || this.parseSupplierId();
            if (!supplierId || this.adding) {
                return;
            }
            this.adding = true;
            try {
                const firstPage = Array.isArray(this.sellerPreview)
                    ? this.sellerPreview.slice(0, 100)
                    : [];
                if (firstPage.length) {
                    const itemsById = new Map(firstPage.map(item => [Number(item.nm_id), item]));
                    let complete = firstPage.length < 100;
                    let browserStopped = false;

                    for (let page = 2; page <= 3 && !complete && itemsById.size < 300; page += 1) {
                        try {
                            const pageItems = await this.fetchSellerCatalogFromBrowser(
                                supplierId, page);
                            for (const item of pageItems) {
                                if (itemsById.size >= 300) break;
                                itemsById.set(Number(item.nm_id), item);
                            }
                            if (pageItems.length < 100) complete = true;
                        } catch (error) {
                            browserStopped = true;
                            break;
                        }
                    }

                    const catalogItems = Array.from(itemsById.values()).slice(0, 300);
                    const ids = catalogItems.map(item => Number(item.nm_id));
                    const added = await this.postProducts({
                        group_id: this.groupId,
                        nm_ids: ids,
                    });
                    this.mergeAddedProducts(added.products, catalogItems);

                    // Отдельный insert-only запрос фиксирует seller source и
                    // оставляет безопасную фоновую очередь на случай, если
                    // браузер получил не все страницы.
                    await this.postProducts({
                        group_id: this.groupId,
                        wb_supplier_id: supplierId,
                    });
                    this.importRequested = true;
                    this.sourceSupplierId = supplierId;

                    const reachedHardCap = ids.length >= 300;
                    if (complete || reachedHardCap) {
                        await this.clearSellerImportRequest();
                    }

                    const remainder = browserStopped && !complete && !reachedHardCap;
                    this.$store.toasts.success(
                        remainder
                            ? `${ids.length} товаров добавлено сразу; остаток повторит scheduler`
                            : `${ids.length} товаров добавлено в мониторинг`,
                        remainder ? 'Каталог добавлен частично' : 'Каталог добавлен'
                    );
                    this.showAddModal = false;
                    this.sellerSelectedIds = [];
                    this.loadCompare();
                    return;
                }

                await this.postProducts({ group_id: this.groupId, wb_supplier_id: supplierId });
                this.importRequested = true;
                this.sourceSupplierId = supplierId;
                this.$store.toasts.success(
                    'WB не отдал превью; scheduler повторит запрос по текущему v4 endpoint',
                    'Каталог оставлен в очереди'
                );
                this.showAddModal = false;
            } catch (error) {
                this.$store.toasts.error(error.message || 'Не удалось запланировать импорт');
            } finally {
                this.adding = false;
            }
        },

        askRemoveProduct(product) {
            this.pendingRemoval = product;
        },

        closeRemoveDialog() {
            if (!this.removing) this.pendingRemoval = null;
        },

        async confirmRemoveProduct() {
            const product = this.pendingRemoval;
            if (!product || this.removing) return;
            this.removing = true;
            try {
                const response = await fetch(`/api/competitors/products/${product.id}`, {
                    method: 'DELETE',
                    headers: { 'X-CSRFToken': this.csrf() },
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) {
                    throw new Error(data.error || 'Не удалось убрать товар');
                }
                this.products = this.products.filter(item => Number(item.id) !== Number(product.id));
                this.pendingRemoval = null;
                this.$store.toasts.success('Товар убран из мониторинга');
                this.loadCompare();
            } catch (error) {
                this.$store.toasts.error(error.message || 'Не удалось убрать товар');
            } finally {
                this.removing = false;
            }
        },

        showHistoryFor(product) {
            this.historyProduct = product;
            this.historyError = null;
            this.historyHasData = false;
            this.loadHistory();
        },

        closeHistory() {
            this.historyProduct = null;
            if (this._chart) {
                this._chart.destroy();
                this._chart = null;
            }
        },

        async loadHistory() {
            const product = this.historyProduct;
            if (!product) return;
            this.historyLoading = true;
            this.historyError = null;
            try {
                const response = await fetch(
                    `/api/competitors/products/${product.id}/history?period=${this.historyPeriod}`);
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || 'Ошибка загрузки истории');
                await this.$nextTick();
                this.renderHistory(data.history || []);
            } catch (error) {
                this.historyError = error.message || 'Ошибка загрузки истории';
                this.historyHasData = false;
                if (this._chart) {
                    this._chart.destroy();
                    this._chart = null;
                }
            } finally {
                this.historyLoading = false;
            }
        },

        renderHistory(history) {
            this.historyHasData = history.length > 0;
            if (this._chart) {
                this._chart.destroy();
                this._chart = null;
            }
            if (!history.length) return;
            const canvas = this.$refs.historyChart;
            if (!canvas || typeof Chart === 'undefined' || !window.shChart) {
                this.historyError = 'График временно недоступен';
                this.historyHasData = false;
                return;
            }

            const labels = history.map(item => {
                const date = this.parseUTC(item.created_at);
                return date
                    ? date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })
                    : '';
            });
            const palette = window.shChart;
            const datasets = [
                {
                    label: 'Цена со скидкой',
                    data: history.map(item => item.sale_price),
                    borderColor: palette.color(1),
                    backgroundColor: palette.fade(palette.color(1), 0.08),
                    fill: true,
                    tension: 0.3,
                    borderWidth: 2,
                    pointRadius: 2,
                    pointHoverRadius: 5,
                    pointBackgroundColor: palette.color(1),
                    spanGaps: true,
                },
                {
                    label: 'Без скидки',
                    data: history.map(item => item.price),
                    borderColor: palette.color(3),
                    borderDash: [4, 4],
                    fill: false,
                    tension: 0.3,
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    spanGaps: true,
                },
            ];
            const ownPrice = this.ownPrice();
            if (ownPrice !== null && ownPrice !== undefined) {
                datasets.push({
                    label: 'Ваша цена',
                    data: labels.map(() => ownPrice),
                    borderColor: palette.color(5),
                    borderDash: [8, 4],
                    fill: false,
                    borderWidth: 2,
                    pointRadius: 0,
                });
            }

            this._chart = palette.register(new Chart(canvas, {
                type: 'line',
                data: { labels, datasets },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { color: palette.textMuted(), usePointStyle: true, boxWidth: 8 },
                        },
                        tooltip: {
                            backgroundColor: palette._read('--bg-sidebar'),
                            titleColor: palette._read('--text-sidebar'),
                            bodyColor: palette._read('--text-sidebar'),
                            cornerRadius: 8,
                            padding: 12,
                        },
                    },
                    scales: {
                        x: {
                            grid: { display: false },
                            ticks: { color: palette.textMuted(), maxTicksLimit: 8 },
                        },
                        y: {
                            beginAtZero: false,
                            grid: { color: palette.grid() },
                            ticks: { color: palette.textMuted() },
                        },
                    },
                },
            }), (chart, colors) => {
                chart.data.datasets[0].borderColor = colors.color(1);
                chart.data.datasets[0].backgroundColor = colors.fade(colors.color(1), 0.08);
                chart.data.datasets[0].pointBackgroundColor = colors.color(1);
                chart.data.datasets[1].borderColor = colors.color(3);
                if (chart.data.datasets[2]) chart.data.datasets[2].borderColor = colors.color(5);
                chart.options.plugins.legend.labels.color = colors.textMuted();
                chart.options.plugins.tooltip.backgroundColor = colors._read('--bg-sidebar');
                chart.options.plugins.tooltip.titleColor = colors._read('--text-sidebar');
                chart.options.plugins.tooltip.bodyColor = colors._read('--text-sidebar');
                chart.options.scales.x.ticks.color = colors.textMuted();
                chart.options.scales.y.ticks.color = colors.textMuted();
                chart.options.scales.y.grid.color = colors.grid();
            });
        },
    };
}

window.competitorGroupDetail = competitorGroupDetail;
