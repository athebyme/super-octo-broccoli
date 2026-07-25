/* Seller Hub · Vue Kit — общий слой страниц на Vue 3 (CDN, без бандлера).
 *
 * Даёт три вещи, из-за которых страницы получаются одинаково опрятными:
 *   1) форматирование фактов (деньги, числа, даты, склонения) в одном месте;
 *   2) сетевой слой, который отличает истёкшую сессию от пустого ответа;
 *   3) набор компонентов «Тёплой редакции»: таблица, карточка, фильтры,
 *      панель массовых действий, боковая панель, пустые состояния.
 *
 * Использование на странице:
 *   shVue.mount('#my-app', { data() {...}, methods: {...} })
 * Компоненты регистрируются автоматически, утилиты доступны как this.$fmt.
 */
(function (global) {
    'use strict';

    /* ==================== Форматирование фактов ==================== */

    function numberOrNull(value) {
        if (value === null || value === undefined || value === '') return null;
        var num = typeof value === 'number' ? value : parseFloat(value);
        return isFinite(num) ? num : null;
    }

    function int(value) {
        var num = numberOrNull(value);
        return num === null ? '—' : new Intl.NumberFormat('ru-RU').format(Math.round(num));
    }

    function money(value, currency) {
        var num = numberOrNull(value);
        if (num === null) return '—';
        var suffix = !currency || currency === 'RUB' ? ' ₽' : ' ' + currency;
        return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(num) + suffix;
    }

    function percent(value, digits) {
        var num = numberOrNull(value);
        if (num === null) return '—';
        return new Intl.NumberFormat('ru-RU', {
            maximumFractionDigits: digits === undefined ? 1 : digits,
        }).format(num) + '%';
    }

    function plural(n, one, few, many) {
        var abs = Math.abs(n) % 100;
        var last = abs % 10;
        if (abs > 10 && abs < 20) return many;
        if (last > 1 && last < 5) return few;
        if (last === 1) return one;
        return many;
    }

    function countWithNoun(n, one, few, many) {
        return int(n) + ' ' + plural(n, one, few, many);
    }

    function parseDate(iso) {
        if (!iso) return null;
        // Сервер отдаёт naive-UTC — без Z браузер прочитает его как локальное время
        var normalized = /Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z';
        var date = new Date(normalized);
        return isNaN(date.getTime()) ? null : date;
    }

    function relTime(iso) {
        var date = parseDate(iso);
        if (!date) return '—';
        var diff = Math.max(0, Date.now() - date.getTime());
        var minutes = Math.floor(diff / 60000);
        if (minutes < 1) return 'только что';
        if (minutes < 60) return minutes + ' мин назад';
        var hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + ' ч назад';
        var days = Math.floor(hours / 24);
        if (days < 7) return days + ' ' + plural(days, 'день', 'дня', 'дней') + ' назад';
        return date.toLocaleDateString('ru-RU');
    }

    function dateTime(iso) {
        var date = parseDate(iso);
        if (!date) return '—';
        return date.toLocaleDateString('ru-RU') + ', ' + date.toLocaleTimeString('ru-RU', {
            hour: '2-digit',
            minute: '2-digit',
        });
    }

    var fmt = {
        int: int,
        money: money,
        percent: percent,
        plural: plural,
        countWithNoun: countWithNoun,
        relTime: relTime,
        dateTime: dateTime,
        numberOrNull: numberOrNull,
    };

    /* ==================== Сетевой слой ==================== */

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    function readResponse(response) {
        if (response.status === 401) {
            var expired = new Error('Сессия истекла — войдите заново, чтобы продолжить');
            expired.code = 'auth_required';
            throw expired;
        }
        var contentType = response.headers.get('content-type') || '';
        if (contentType.indexOf('application/json') === -1) {
            if (response.redirected || contentType.indexOf('text/html') !== -1) {
                var html = new Error('Сессия истекла — войдите заново, чтобы продолжить');
                html.code = 'auth_required';
                throw html;
            }
        }
        return response.json().catch(function () {
            return {};
        }).then(function (data) {
            if (!response.ok || data.success === false) {
                var error = new Error(
                    data.error || ('Не удалось загрузить данные (HTTP ' + response.status + ')')
                );
                error.code = data.code || 'request_failed';
                error.status = response.status;
                throw error;
            }
            return data;
        });
    }

    function buildUrl(url, params) {
        if (!params) return url;
        var search = new URLSearchParams();
        Object.keys(params).forEach(function (key) {
            var value = params[key];
            if (value === null || value === undefined || value === '' || value === false) return;
            search.set(key, value === true ? '1' : value);
        });
        var query = search.toString();
        return query ? url + (url.indexOf('?') === -1 ? '?' : '&') + query : url;
    }

    var api = {
        get: function (url, params, options) {
            var opts = options || {};
            return fetch(buildUrl(url, params), {
                headers: { Accept: 'application/json' },
                signal: opts.signal,
            }).then(readResponse);
        },
        post: function (url, body, options) {
            var opts = options || {};
            return fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    'X-CSRFToken': csrfToken(),
                },
                body: JSON.stringify(body || {}),
                signal: opts.signal,
            }).then(readResponse);
        },
        buildUrl: buildUrl,
    };

    /* ==================== Компоненты ==================== */

    var components = {};

    /* Плитка показателя: крупное число, подпись, необязательная динамика */
    components['sh-stat'] = {
        props: {
            label: { type: String, required: true },
            value: { type: [String, Number], default: '—' },
            hint: { type: String, default: '' },
            trend: { type: Number, default: null },
            tone: { type: String, default: '' },
            href: { type: String, default: '' },
            loading: { type: Boolean, default: false },
        },
        computed: {
            trendClass: function () {
                if (this.trend === null) return '';
                return this.trend >= 0 ? 'is-up' : 'is-down';
            },
            trendText: function () {
                if (this.trend === null) return '';
                var sign = this.trend > 0 ? '+' : '';
                return sign + fmt.percent(this.trend);
            },
        },
        template: `
            <component :is="href ? 'a' : 'div'" :href="href || null"
                       class="shk-stat" :class="[tone ? 'shk-stat--' + tone : '', href ? 'shk-stat--link' : '']">
                <span class="shk-stat-label">{{ label }}</span>
                <span v-if="loading" class="sh-skeleton sh-skeleton--line-50" style="height:26px"></span>
                <span v-else class="shk-stat-value">{{ value }}</span>
                <span class="shk-stat-foot">
                    <span v-if="trend !== null" class="shk-stat-trend" :class="trendClass">{{ trendText }}</span>
                    <span v-if="hint" class="shk-stat-hint">{{ hint }}</span>
                </span>
            </component>
        `,
    };

    /* Строка фильтров: поиск с хоткеем «/», чипы, произвольные контролы */
    components['sh-filter-bar'] = {
        props: {
            modelValue: { type: String, default: '' },
            placeholder: { type: String, default: 'Поиск' },
            chips: { type: Array, default: function () { return []; } },
            activeChip: { type: [String, Number], default: '' },
            sticky: { type: Boolean, default: true },
        },
        emits: ['update:modelValue', 'chip'],
        mounted: function () {
            this._onKey = this.onKey.bind(this);
            window.addEventListener('keydown', this._onKey);
        },
        beforeUnmount: function () {
            window.removeEventListener('keydown', this._onKey);
        },
        methods: {
            onKey: function (event) {
                var target = event.target;
                if (target && /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) return;
                if (event.key === '/') {
                    event.preventDefault();
                    if (this.$refs.search) this.$refs.search.focus();
                }
            },
        },
        template: `
            <div class="shk-filters" :class="{'is-sticky': sticky}">
                <label class="shk-search">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
                    <input ref="search" type="search" :placeholder="placeholder" :value="modelValue"
                           :aria-label="placeholder"
                           @input="$emit('update:modelValue', $event.target.value)"
                           @keydown.esc.prevent="$emit('update:modelValue', '')">
                    <kbd v-show="!modelValue">/</kbd>
                </label>
                <div v-if="chips.length" class="shk-chips" role="group">
                    <button v-for="chip in chips" :key="chip.key" type="button"
                            class="sh-chip sh-chip--clickable"
                            :class="{'sh-chip--active': activeChip === chip.value, 'shk-chip--alarm': chip.alarm && chip.count}"
                            @click="$emit('chip', chip.value)">
                        {{ chip.label }}
                        <span v-if="chip.count !== undefined && chip.count !== null" class="shk-chip-n">{{ chip.count }}</span>
                    </button>
                </div>
                <div class="shk-filters-tools"><slot></slot></div>
            </div>
        `,
    };

    /* Таблица: колонки, выбор строк, сортировка, скелет, пустое состояние */
    components['sh-data-table'] = {
        props: {
            columns: { type: Array, required: true },
            rows: { type: Array, default: function () { return []; } },
            rowKey: { type: String, default: 'id' },
            loading: { type: Boolean, default: false },
            selectable: { type: Boolean, default: false },
            selected: { type: Array, default: function () { return []; } },
            sortKey: { type: String, default: '' },
            sortDir: { type: String, default: 'desc' },
            emptyTitle: { type: String, default: 'Здесь пока пусто' },
            emptyText: { type: String, default: '' },
        },
        emits: ['update:selected', 'row-click', 'sort'],
        computed: {
            allSelected: function () {
                return this.rows.length > 0 && this.selected.length === this.rows.length;
            },
            someSelected: function () {
                return this.selected.length > 0 && !this.allSelected;
            },
        },
        methods: {
            keyOf: function (row) { return row[this.rowKey]; },
            isSelected: function (row) { return this.selected.indexOf(this.keyOf(row)) !== -1; },
            toggleRow: function (row) {
                var key = this.keyOf(row);
                var next = this.selected.slice();
                var index = next.indexOf(key);
                if (index === -1) next.push(key); else next.splice(index, 1);
                this.$emit('update:selected', next);
            },
            toggleAll: function () {
                var self = this;
                this.$emit(
                    'update:selected',
                    this.allSelected ? [] : this.rows.map(function (row) { return self.keyOf(row); })
                );
            },
            sortBy: function (column) {
                if (!column.sortable) return;
                var dir = this.sortKey === column.key && this.sortDir === 'desc' ? 'asc' : 'desc';
                this.$emit('sort', { key: column.key, dir: dir });
            },
            cellValue: function (row, column) {
                var value = row[column.key];
                if (column.format === 'money') return fmt.money(value, row.currency);
                if (column.format === 'int') return fmt.int(value);
                if (column.format === 'percent') return fmt.percent(value);
                if (column.format === 'relTime') return fmt.relTime(value);
                if (column.format === 'dateTime') return fmt.dateTime(value);
                return value === null || value === undefined || value === '' ? '—' : value;
            },
        },
        template: `
            <div class="shk-table-wrap">
                <table class="shk-table">
                    <thead>
                        <tr>
                            <th v-if="selectable" class="shk-th-check">
                                <input type="checkbox" :checked="allSelected"
                                       :indeterminate.prop="someSelected"
                                       @change="toggleAll" aria-label="Выбрать все строки">
                            </th>
                            <th v-for="column in columns" :key="column.key"
                                :class="[column.align === 'right' ? 'shk-th-num' : '', column.sortable ? 'is-sortable' : '']"
                                :style="column.width ? { width: column.width } : null"
                                @click="sortBy(column)">
                                {{ column.title }}
                                <span v-if="column.sortable && sortKey === column.key" class="shk-sort">{{ sortDir === 'desc' ? '↓' : '↑' }}</span>
                            </th>
                        </tr>
                    </thead>
                    <tbody v-if="loading && !rows.length">
                        <tr v-for="n in 8" :key="'sk' + n" class="shk-row-skeleton">
                            <td v-if="selectable"></td>
                            <td v-for="column in columns" :key="column.key"><span class="sh-skeleton sh-skeleton--line-70"></span></td>
                        </tr>
                    </tbody>
                    <tbody v-else>
                        <tr v-for="row in rows" :key="keyOf(row)"
                            :class="{'is-selected': isSelected(row)}"
                            tabindex="0"
                            @click="$emit('row-click', row)"
                            @keydown.enter.prevent="$emit('row-click', row)">
                            <td v-if="selectable" class="shk-td-check" @click.stop>
                                <input type="checkbox" :checked="isSelected(row)" @change="toggleRow(row)"
                                       :aria-label="'Выбрать строку'">
                            </td>
                            <td v-for="column in columns" :key="column.key"
                                :class="column.align === 'right' ? 'shk-td-num' : ''">
                                <slot :name="'cell-' + column.key" :row="row" :value="row[column.key]">
                                    {{ cellValue(row, column) }}
                                </slot>
                            </td>
                        </tr>
                    </tbody>
                </table>
                <div v-if="!loading && !rows.length" class="shk-empty">
                    <p class="shk-empty-title">{{ emptyTitle }}</p>
                    <p v-if="emptyText" class="shk-empty-text">{{ emptyText }}</p>
                    <slot name="empty-action"></slot>
                </div>
            </div>
        `,
    };

    /* Панель массовых действий — появляется, когда что-то выбрано */
    components['sh-bulk-bar'] = {
        props: {
            count: { type: Number, default: 0 },
            noun: { type: Array, default: function () { return ['товар', 'товара', 'товаров']; } },
        },
        emits: ['clear'],
        computed: {
            label: function () {
                return fmt.countWithNoun(this.count, this.noun[0], this.noun[1], this.noun[2]);
            },
        },
        template: `
            <transition name="shk-rise">
                <div v-if="count > 0" class="shk-bulkbar" role="region" aria-label="Действия с выбранным">
                    <span class="shk-bulkbar-count">Выбрано {{ label }}</span>
                    <div class="shk-bulkbar-actions"><slot></slot></div>
                    <button type="button" class="shk-bulkbar-clear" @click="$emit('clear')">Снять выбор</button>
                </div>
            </transition>
        `,
    };

    /* Боковая панель с ловушкой фокуса и Esc */
    components['sh-drawer'] = {
        props: {
            open: { type: Boolean, default: false },
            title: { type: String, default: '' },
            width: { type: String, default: '430px' },
        },
        emits: ['close'],
        watch: {
            open: function (value) {
                document.body.style.overflow = value ? 'hidden' : '';
                if (value) {
                    var self = this;
                    this.$nextTick(function () {
                        if (self.$refs.panel) self.$refs.panel.focus();
                    });
                }
            },
        },
        mounted: function () {
            this._onKey = this.onKey.bind(this);
            window.addEventListener('keydown', this._onKey);
        },
        beforeUnmount: function () {
            window.removeEventListener('keydown', this._onKey);
            document.body.style.overflow = '';
        },
        methods: {
            onKey: function (event) {
                if (!this.open) return;
                if (event.key === 'Escape') {
                    event.preventDefault();
                    this.$emit('close');
                    return;
                }
                if (event.key !== 'Tab') return;
                var root = this.$refs.panel;
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
                } else if (event.shiftKey && active === first) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && active === last) {
                    event.preventDefault();
                    first.focus();
                }
            },
        },
        template: `
            <teleport to="body">
                <transition name="shk-fade">
                    <div v-if="open" class="shk-backdrop" @click="$emit('close')" aria-hidden="true"></div>
                </transition>
                <transition name="shk-slide">
                    <aside v-if="open" ref="panel" class="shk-drawer" :style="{ width: 'min(' + width + ', 100vw)' }"
                           role="dialog" aria-modal="true" :aria-label="title" tabindex="-1">
                        <header class="shk-drawer-head">
                            <h2 class="shk-drawer-title">{{ title }}</h2>
                            <slot name="head"></slot>
                            <button type="button" class="sh-icon-btn" @click="$emit('close')" title="Закрыть (Esc)">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>
                            </button>
                        </header>
                        <div class="shk-drawer-body"><slot></slot></div>
                        <footer v-if="$slots.foot" class="shk-drawer-foot"><slot name="foot"></slot></footer>
                    </aside>
                </transition>
            </teleport>
        `,
    };

    /* Пустое состояние: заголовок, объяснение и один следующий шаг */
    components['sh-empty'] = {
        props: {
            title: { type: String, required: true },
            text: { type: String, default: '' },
        },
        template: `
            <div class="shk-empty">
                <p class="shk-empty-title">{{ title }}</p>
                <p v-if="text" class="shk-empty-text">{{ text }}</p>
                <slot></slot>
            </div>
        `,
    };

    /* Баннер ошибки с повтором — вместо молчаливого пустого экрана */
    components['sh-error'] = {
        props: {
            message: { type: String, default: '' },
            retryLabel: { type: String, default: 'Повторить' },
        },
        emits: ['retry'],
        template: `
            <div v-if="message" class="shk-error" role="alert">
                <span>{{ message }}</span>
                <button type="button" class="sh-btn sh-btn--secondary" @click="$emit('retry')">{{ retryLabel }}</button>
            </div>
        `,
    };

    /* Меню массовых действий: сгруппированное, с опасными внизу.
       Пункт всегда говорит, к скольким товарам применится. */
    components['sh-action-menu'] = {
        props: {
            groups: { type: Array, required: true },
            count: { type: Number, default: 0 },
            label: { type: String, default: 'Действия' },
            disabled: { type: Boolean, default: false },
        },
        emits: ['pick'],
        data: function () { return { open: false }; },
        mounted: function () {
            this._onDoc = this.onDocClick.bind(this);
            document.addEventListener('click', this._onDoc);
        },
        beforeUnmount: function () {
            document.removeEventListener('click', this._onDoc);
        },
        methods: {
            onDocClick: function (event) {
                if (this.$el && !this.$el.contains(event.target)) this.open = false;
            },
            pick: function (action) {
                if (action.disabled) return;
                this.open = false;
                this.$emit('pick', action);
            },
        },
        template: `
            <div class="shk-actions">
                <button type="button" class="sh-btn sh-btn--primary sh-btn--sm"
                        :disabled="disabled || !count" @click.stop="open = !open"
                        :aria-expanded="open ? 'true' : 'false'">
                    {{ label }}
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" class="shk-actions-caret"><path d="m6 9 6 6 6-6"/></svg>
                </button>
                <transition name="shk-fade">
                    <div v-if="open" class="shk-actions-menu" role="menu">
                        <div v-for="group in groups" :key="group.title" class="shk-actions-group"
                             :class="{'is-danger': group.danger}">
                            <span class="shk-actions-group-title">{{ group.title }}</span>
                            <button v-for="action in group.items" :key="action.key" type="button"
                                    class="shk-actions-item" :class="{'is-danger': action.danger}"
                                    :disabled="action.disabled" role="menuitem"
                                    @click="pick(action)">
                                <span class="shk-actions-item-label">{{ action.label }}</span>
                                <span v-if="action.hint" class="shk-actions-item-hint">{{ action.hint }}</span>
                            </button>
                        </div>
                    </div>
                </transition>
            </div>
        `,
    };

    /* Подтверждение, которое называет последствие, а не спрашивает «уверены?» */
    components['sh-confirm'] = {
        props: {
            open: { type: Boolean, default: false },
            title: { type: String, default: 'Подтвердите действие' },
            message: { type: String, default: '' },
            details: { type: Array, default: function () { return []; } },
            confirmLabel: { type: String, default: 'Продолжить' },
            danger: { type: Boolean, default: false },
            busy: { type: Boolean, default: false },
        },
        emits: ['confirm', 'cancel'],
        watch: {
            open: function (value) {
                if (!value) return;
                var self = this;
                this.$nextTick(function () {
                    if (self.$refs.confirmBtn) self.$refs.confirmBtn.focus();
                });
            },
        },
        mounted: function () {
            this._onKey = this.onKey.bind(this);
            window.addEventListener('keydown', this._onKey);
        },
        beforeUnmount: function () {
            window.removeEventListener('keydown', this._onKey);
        },
        methods: {
            onKey: function (event) {
                if (this.open && event.key === 'Escape') {
                    event.preventDefault();
                    this.$emit('cancel');
                }
            },
        },
        template: `
            <teleport to="body">
                <transition name="shk-fade">
                    <div v-if="open" class="shk-backdrop" @click="$emit('cancel')"></div>
                </transition>
                <transition name="shk-rise">
                    <div v-if="open" class="shk-confirm" role="alertdialog" aria-modal="true">
                        <h3 class="shk-confirm-title">{{ title }}</h3>
                        <p v-if="message" class="shk-confirm-text">{{ message }}</p>
                        <ul v-if="details.length" class="shk-confirm-list">
                            <li v-for="(line, i) in details" :key="i">{{ line }}</li>
                        </ul>
                        <slot></slot>
                        <div class="shk-confirm-actions">
                            <button type="button" class="sh-btn sh-btn--secondary" @click="$emit('cancel')">Отмена</button>
                            <button ref="confirmBtn" type="button"
                                    class="sh-btn" :class="[danger ? 'sh-btn--danger' : 'sh-btn--primary', busy ? 'is-loading' : '']"
                                    :disabled="busy" @click="$emit('confirm')">{{ confirmLabel }}</button>
                        </div>
                    </div>
                </transition>
            </teleport>
        `,
    };

    /* Ход фоновой задачи: прогресс, счётчики, честный итог */
    components['sh-job-progress'] = {
        props: {
            job: { type: Object, default: null },
            title: { type: String, default: 'Выполняем' },
        },
        emits: ['retry-failed', 'dismiss'],
        computed: {
            percent: function () {
                if (!this.job) return 0;
                if (this.job.progress_pct !== undefined && this.job.progress_pct !== null) {
                    return Math.max(0, Math.min(100, this.job.progress_pct));
                }
                var total = this.job.total || 0;
                return total ? Math.round((this.job.processed || 0) / total * 100) : 0;
            },
            finished: function () {
                return !!this.job && (this.job.status === 'done' || this.job.status === 'failed');
            },
            tone: function () {
                if (!this.job) return '';
                if (this.job.status === 'failed') return 'danger';
                if (this.finished && (this.job.failed || this.job.conflicted)) return 'warn';
                if (this.finished) return 'ok';
                return '';
            },
        },
        template: `
            <div v-if="job" class="shk-job" :class="tone ? 'shk-job--' + tone : ''">
                <div class="shk-job-head">
                    <span class="shk-job-title">{{ finished ? (job.status === 'failed' ? 'Остановлено' : 'Готово') : title }}</span>
                    <span class="shk-job-count">{{ job.processed || 0 }} из {{ job.total || 0 }}</span>
                    <button v-if="finished" type="button" class="shk-job-close" @click="$emit('dismiss')" title="Скрыть">✕</button>
                </div>
                <div class="sh-progress" :class="tone ? 'sh-progress--' + tone : ''" role="progressbar"
                     :aria-valuenow="percent" aria-valuemin="0" aria-valuemax="100">
                    <div class="sh-progress-bar" :style="{ width: percent + '%' }"></div>
                </div>
                <p v-if="job.message" class="shk-job-message">{{ job.message }}</p>
                <div v-if="finished" class="shk-job-result">
                    <span v-if="job.succeeded" class="shk-job-stat is-ok">Успешно: {{ job.succeeded }}</span>
                    <span v-if="job.skipped" class="shk-job-stat">Пропущено: {{ job.skipped }}</span>
                    <span v-if="job.failed" class="shk-job-stat is-danger">С ошибкой: {{ job.failed }}</span>
                    <button v-if="job.failed" type="button" class="sh-btn sh-btn--secondary sh-btn--sm"
                            @click="$emit('retry-failed')">Повторить неудачные</button>
                </div>
            </div>
        `,
    };

    /* Бейдж статуса на семантических токенах */
    components['sh-badge'] = {
        props: {
            label: { type: String, required: true },
            tone: { type: String, default: 'muted' },
            title: { type: String, default: '' },
        },
        template: `<span class="shk-badge" :class="'shk-badge--' + tone" :title="title || null">{{ label }}</span>`,
    };

    /* ==================== Сборка приложения ==================== */

    function mount(selector, options) {
        var el = document.querySelector(selector);
        if (!el || typeof Vue === 'undefined') return null;
        var app = Vue.createApp(options || {});
        Object.keys(components).forEach(function (name) {
            app.component(name, components[name]);
        });
        app.config.globalProperties.$fmt = fmt;
        app.config.globalProperties.$api = api;
        var instance = app.mount(selector);
        el.removeAttribute('v-cloak');
        return instance;
    }

    function bootstrapData(elementId) {
        var node = document.getElementById(elementId);
        if (!node) return {};
        try {
            return JSON.parse(node.textContent);
        } catch (err) {
            return {};
        }
    }

    global.shVue = {
        mount: mount,
        bootstrapData: bootstrapData,
        components: components,
        fmt: fmt,
        api: api,
    };
})(window);
