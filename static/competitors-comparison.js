/* global Alpine */
function competitorComparison() {
    return {
        data: null,
        loading: false,
        error: null,
        groupId: '',
        query: '',
        scope: 'all',
        sort: 'coverage',
        page: 1,
        perPage: 24,
        expanded: {},
        offerPreviewLimit: 4,
        requestSequence: 0,
        abortController: null,

        init() {
            const params = new URLSearchParams(window.location.search);
            this.groupId = params.get('group_id') || '';
            this.query = params.get('q') || '';
            this.scope = ['all', 'linked', 'missing'].includes(params.get('scope'))
                ? params.get('scope') : 'all';
            this.sort = ['coverage', 'gap', 'price', 'name'].includes(params.get('sort'))
                ? params.get('sort') : 'coverage';
            const requestedPage = Number.parseInt(params.get('page') || '1', 10);
            this.page = Number.isInteger(requestedPage) && requestedPage > 0
                ? requestedPage : 1;
            this.load();
        },

        summary() {
            return this.data?.summary || {
                identities: 0, with_own: 0, without_own: 0,
                offers: 0, competitors: 0, undercut: 0,
                undercut_base: 0, undercut_final: 0,
                ours_lowest_or_equal: 0, awaiting_match: 0,
                excluded_non_exact: 0,
            };
        },

        groups() {
            return Array.isArray(this.data?.groups) ? this.data.groups : [];
        },

        items() {
            return Array.isArray(this.data?.items) ? this.data.items : [];
        },

        pagination() {
            return this.data?.pagination || {
                page: this.page, pages: 1, total: 0,
                has_previous: false, has_next: false,
            };
        },

        selectedGroupName() {
            if (!this.groupId) return '';
            return this.groups().find(group => String(group.id) === String(this.groupId))?.name || '';
        },

        buildParams() {
            const params = new URLSearchParams();
            if (this.groupId) params.set('group_id', this.groupId);
            if (this.query.trim()) params.set('q', this.query.trim());
            if (this.scope !== 'all') params.set('scope', this.scope);
            if (this.sort !== 'coverage') params.set('sort', this.sort);
            if (this.page > 1) params.set('page', String(this.page));
            params.set('per_page', String(this.perPage));
            return params;
        },

        syncLocation() {
            const params = this.buildParams();
            params.delete('per_page');
            const suffix = params.toString();
            window.history.replaceState(
                {}, '', `/competitors/comparison${suffix ? `?${suffix}` : ''}`,
            );
        },

        async load() {
            const sequence = ++this.requestSequence;
            if (this.abortController) this.abortController.abort();
            this.abortController = new AbortController();
            this.loading = true;
            this.error = null;
            this.syncLocation();
            try {
                const response = await fetch(
                    `/api/competitors/comparison?${this.buildParams().toString()}`,
                    { signal: this.abortController.signal },
                );
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(payload.error || 'Сервис сравнения временно недоступен');
                }
                if (sequence !== this.requestSequence) return;
                this.data = payload;
                this.page = payload.pagination?.page || 1;
                this.expanded = {};
                this.syncLocation();
            } catch (error) {
                if (error?.name === 'AbortError') return;
                if (sequence === this.requestSequence) {
                    this.error = error?.message || 'Не удалось загрузить сравнение';
                }
            } finally {
                if (sequence === this.requestSequence) this.loading = false;
            }
        },

        applyFilters() {
            this.page = 1;
            this.load();
        },

        setScope(value) {
            if (this.scope === value) return;
            this.scope = value;
            this.applyFilters();
        },

        goPage(value) {
            const page = Number(value);
            if (!Number.isInteger(page) || page < 1 || page > this.pagination().pages) return;
            this.page = page;
            this.load().then(() => {
                window.scrollTo({ top: 0, behavior: 'smooth' });
            });
        },

        visibleOffers(item) {
            const offers = Array.isArray(item?.offers) ? item.offers : [];
            return this.expanded[item?.supplier?.id]
                ? offers : offers.slice(0, this.offerPreviewLimit);
        },

        toggleExpanded(supplierId) {
            this.expanded = {
                ...this.expanded,
                [supplierId]: !this.expanded[supplierId],
            };
        },

        priceLanes(item) {
            const lanes = item?.metrics?.price_lanes || {};
            const empty = {
                min_competitor_price: null,
                median_competitor_price: null,
                max_competitor_price: null,
                own_position: null,
                total_with_own: null,
                own_vs_min_percent: null,
                competitor_spread_percent: null,
            };
            return [
                {
                    key: 'base',
                    label: 'До скидок',
                    hint: 'перечёркнутая basic',
                    ownPrice: item?.own_product?.base_price ?? null,
                    ownSource: item?.own_product?.base_price_source ?? null,
                    metrics: lanes.base || empty,
                },
                {
                    key: 'final',
                    label: 'Витринная',
                    hint: 'публичная total / product',
                    ownPrice: item?.own_product?.final_price ?? null,
                    ownSource: item?.own_product?.final_price_source ?? null,
                    metrics: lanes.final || empty,
                },
            ];
        },

        offerTone(offer) {
            const delta = offer?.final_delta_to_own_percent
                ?? offer?.base_delta_to_own_percent;
            if (delta === null || delta === undefined) return '';
            if (Number(delta) < 0) return 'is-cheaper';
            if (Number(delta) > 0) return 'is-expensive';
            return '';
        },

        gapTone(value) {
            if (value === null || value === undefined) return 'is-neutral';
            if (Number(value) > 0) return 'is-bad';
            if (Number(value) < 0) return 'is-good';
            return 'is-neutral';
        },

        ownGapText(value) {
            if (value === null || value === undefined) return '';
            const number = Number(value);
            if (number > 0) return `${this.fmtPercent(number)} выше минимума`;
            if (number < 0) return `${this.fmtPercent(Math.abs(number))} ниже минимума`;
            return 'на уровне минимума';
        },

        offerDeltaText(value) {
            if (value === null || value === undefined) return '';
            const number = Number(value);
            if (number < 0) return `${this.fmtPercent(Math.abs(number))} дешевле нас`;
            if (number > 0) return `${this.fmtPercent(number)} дороже нас`;
            return 'как у нас';
        },

        fmtPercent(value) {
            const number = Number(value);
            if (!Number.isFinite(number)) return '—';
            return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(number)}%`;
        },

        fmtPrice(value) {
            if (value === null || value === undefined || Number(value) <= 0) return 'цены нет';
            return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(Number(value))} ₽`;
        },

        fmtNumber(value) {
            return new Intl.NumberFormat('ru-RU').format(Number(value) || 0);
        },

        freshness(value) {
            if (!value) return 'цена не наблюдалась';
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) return 'дата неизвестна';
            const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
            if (seconds < 90) return 'только что';
            if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
            if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч назад`;
            return `${Math.floor(seconds / 86400)} дн назад`;
        },

        plural(number, forms) {
            const value = Math.abs(Number(number) || 0) % 100;
            const last = value % 10;
            if (value > 10 && value < 20) return forms[2];
            if (last > 1 && last < 5) return forms[1];
            if (last === 1) return forms[0];
            return forms[2];
        },

        emptyTitle() {
            if (this.query.trim() || this.scope !== 'all') return 'По фильтрам ничего не нашлось';
            if (this.summary().awaiting_match > 0) return 'Сопоставление ещё продолжается';
            return 'Точных общих товаров пока нет';
        },

        emptyText() {
            if (this.query.trim() || this.scope !== 'all') {
                return 'Очистите поиск или выберите другой режим наличия нашей карточки.';
            }
            if (this.summary().awaiting_match > 0) {
                return `В очереди ещё ${this.summary().awaiting_match}. Точные совпадения будут появляться здесь автоматически.`;
            }
            return 'Запустите поиск соответствий в группах продавцов или подтвердите точные совпадения вручную.';
        },
    };
}
