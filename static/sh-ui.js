/* ============================================================
   Seller Hub — sh-ui.js
   Клиентские контроллеры дизайн-системы «Тёплая редакция».
   Загружается ДО Alpine core (defer, по порядку), поэтому
   x-data="shCmdPalette()" и Alpine.store(...) доступны вовремя.
   ============================================================ */
(function () {
    'use strict';

    /* ─────────────────────────────────────────────
       Command palette — фильтрация и навигация ↑↓/Enter
       поверх статического списка ссылок .sh-cmdpal-item
       ───────────────────────────────────────────── */
    window.shCmdPalette = function () {
        return {
            query: '',
            activeIndex: -1,
            empty: false,
            items: [],
            _seq: 0,
            _timer: null,

            // Собрать видимые пункты (разделы + товары) в порядке DOM
            collect() {
                this.items = Array.from(this.$root.querySelectorAll('.sh-cmdpal-item'))
                    .filter(el => el.style.display !== 'none');
                if (this.activeIndex >= this.items.length) this.activeIndex = this.items.length - 1;
                if (this.activeIndex < 0 && this.items.length) this.activeIndex = 0;
                this.updateEmpty();
                this.highlight();
            },

            // Открытие палитры / полный сброс
            refresh() {
                this.query = '';
                this.clearProducts();
                this.filter();
            },

            filter() {
                this.filterStatic();
                this.searchProducts();
            },

            // Фильтрация статических разделов по подстроке
            filterStatic() {
                const q = (this.query || '').trim().toLowerCase();
                this.$root.querySelectorAll('.sh-cmdpal-item:not([data-cmd-product])').forEach(function (el) {
                    const match = !q || (el.textContent || '').toLowerCase().indexOf(q) !== -1;
                    el.style.display = match ? '' : 'none';
                });
                this.$root.querySelectorAll('.sh-cmdpal-group:not(.sh-cmdpal-products-group)').forEach(function (g) {
                    let sib = g.nextElementSibling, vis = false;
                    while (sib && !sib.classList.contains('sh-cmdpal-group')) {
                        if (sib.classList.contains('sh-cmdpal-item') && !sib.hasAttribute('data-cmd-product') && sib.style.display !== 'none') { vis = true; break; }
                        sib = sib.nextElementSibling;
                    }
                    g.style.display = vis ? '' : 'none';
                });
                this.activeIndex = 0;
                this.collect();
            },

            // Debounced поиск товаров через API (гонки гасятся seq)
            searchProducts() {
                const q = (this.query || '').trim();
                if (this._timer) clearTimeout(this._timer);
                if (q.length < 2) { this._seq++; this.clearProducts(); this.collect(); return; }
                const self = this;
                const seq = ++this._seq;
                this._timer = setTimeout(function () {
                    fetch('/api/products/search?q=' + encodeURIComponent(q))
                        .then(r => (r.ok ? r.json() : { items: [] }))
                        .then(d => { if (seq === self._seq) self.renderProducts(d.items || []); })
                        .catch(() => { if (seq === self._seq) self.renderProducts([]); });
                }, 250);
            },

            renderProducts(list) {
                const wrap = this.$root.querySelector('[data-cmd-products]');
                const group = this.$root.querySelector('.sh-cmdpal-products-group');
                if (!wrap) return;
                wrap.innerHTML = '';
                if (group) group.style.display = list.length ? '' : 'none';
                list.forEach(function (p) {
                    const a = document.createElement('a');
                    a.href = p.url || '#';
                    a.className = 'sh-cmdpal-item';
                    a.setAttribute('data-cmd-product', '');
                    const label = document.createElement('span');
                    label.className = 'sh-cmdpal-item-label';
                    label.textContent = p.title || p.vendor_code || ('nmID ' + (p.nm_id || ''));
                    a.appendChild(label);
                    const hintText = [p.vendor_code, p.nm_id].filter(Boolean).join(' · ');
                    if (hintText) {
                        const hint = document.createElement('span');
                        hint.className = 'sh-cmdpal-item-hint';
                        hint.textContent = hintText;
                        a.appendChild(hint);
                    }
                    wrap.appendChild(a);
                });
                this.collect();
            },

            clearProducts() {
                const wrap = this.$root.querySelector('[data-cmd-products]');
                const group = this.$root.querySelector('.sh-cmdpal-products-group');
                if (wrap) wrap.innerHTML = '';
                if (group) group.style.display = 'none';
            },

            updateEmpty() {
                this.empty = !this.items.length && (this.query || '').trim().length > 0;
            },

            highlight() {
                this.$root.querySelectorAll('.sh-cmdpal-item.active').forEach(el => el.classList.remove('active'));
                const el = this.items[this.activeIndex];
                if (el) { el.classList.add('active'); el.scrollIntoView({ block: 'nearest' }); }
            },

            move(dir) {
                if (!this.items.length) return;
                this.activeIndex = (((this.activeIndex + dir) % this.items.length) + this.items.length) % this.items.length;
                this.highlight();
            },

            choose() {
                const el = this.items[this.activeIndex];
                if (el) el.click();
            }
        };
    };

    /* ─────────────────────────────────────────────
       Звук уведомлений (Web Audio) с mute
       ───────────────────────────────────────────── */
    const chime = {
        _ctx: null,
        _getCtx() {
            if (!this._ctx) this._ctx = new (window.AudioContext || window.webkitAudioContext)();
            if (this._ctx.state === 'suspended') this._ctx.resume();
            return this._ctx;
        },
        play(category) {
            try {
                const ctx = this._getCtx();
                const now = ctx.currentTime, vol = 0.14;
                if (category === 'warning') {
                    this._tone(ctx, now, 440, 0.12, vol);
                    this._tone(ctx, now + 0.15, 392, 0.15, vol);
                } else if (category === 'error' || category === 'danger') {
                    this._tone(ctx, now, 523, 0.10, vol);
                    this._tone(ctx, now + 0.12, 440, 0.10, vol);
                    this._tone(ctx, now + 0.24, 349, 0.16, vol);
                } else {
                    this._tone(ctx, now, 587, 0.12, vol);
                    this._tone(ctx, now + 0.12, 784, 0.12, vol);
                    this._tone(ctx, now + 0.24, 880, 0.24, vol);
                }
            } catch (e) {}
        },
        _tone(ctx, when, freq, dur, vol) {
            const osc = ctx.createOscillator(), gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(0, when);
            gain.gain.linearRampToValueAtTime(vol, when + 0.015);
            gain.gain.exponentialRampToValueAtTime(0.001, when + dur);
            osc.connect(gain); gain.connect(ctx.destination);
            osc.start(when); osc.stop(when + dur + 0.05);
        }
    };
    window._notifChime = chime;

    /* ─────────────────────────────────────────────
       shChart — токен-палитра для Chart.js/inline-SVG,
       перекраска графиков при смене темы (sh-theme-change)
       ───────────────────────────────────────────── */
    window.shChart = {
        _read(name, fallback) {
            try {
                const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
                return v || fallback;
            } catch (e) { return fallback; }
        },
        palette() {
            const out = [];
            for (let i = 1; i <= 6; i++) out.push(this._read('--chart-' + i, '#888888'));
            return out;
        },
        color(i) { return this.palette()[(((i - 1) % 6) + 6) % 6]; },
        text()      { return this._read('--text', '#1a1a1a'); },
        textMuted() { return this._read('--text-muted', '#9a9a9a'); },
        grid()      { return this._read('--border-light', '#eeeeee'); },
        surface()   { return this._read('--bg-card', '#ffffff'); },
        border()    { return this._read('--border', '#e8e5e1'); },
        fade(hex, alpha) {
            const a = alpha == null ? 0.1 : alpha;
            hex = (hex || '').trim().replace('#', '');
            if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
            const n = parseInt(hex, 16);
            if (isNaN(n) || hex.length !== 6) return 'rgba(136,136,136,' + a + ')';
            return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
        },
        _charts: [],
        register(chart, recolor) {
            if (!chart || typeof recolor !== 'function') return chart;
            try { recolor(chart, this); if (chart.update) chart.update('none'); } catch (e) {}
            this._charts.push({ chart, recolor });
            return chart;
        },
        onThemeChange() {
            const self = this;
            this._charts = this._charts.filter(c => c.chart && (c.chart.ctx || c.chart.canvas));
            this._charts.forEach(function (c) {
                try { c.recolor(c.chart, self); if (c.chart.update) c.chart.update('none'); } catch (e) {}
            });
        }
    };
    window.addEventListener('sh-theme-change', function () { window.shChart.onThemeChange(); });

    function plural(n, forms) {
        const m10 = n % 10, m100 = n % 100;
        if (m10 === 1 && m100 !== 11) return forms[0];
        if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return forms[1];
        return forms[2];
    }
    function parseUtc(iso) {
        if (!iso) return null;
        const hasTz = /[zZ]$|[+-]\d\d:?\d\d$/.test(iso);
        const d = new Date(hasTz ? iso : iso + 'Z');
        return isNaN(d.getTime()) ? null : d;
    }

    document.addEventListener('alpine:init', function () {
        /* ── Единый стор тостов (.sh-toast) ── */
        Alpine.store('toasts', {
            items: [],
            _counter: 0,
            add(type, message, title, opts) {
                const o = (typeof opts === 'number') ? { duration: opts } : (opts || {});
                const duration = o.duration != null ? o.duration
                    : (type === 'error' ? 8000 : type === 'warning' ? 6000 : 5000);
                const id = ++this._counter;
                const toast = {
                    id, type: type || 'info', message, title: title || null,
                    link: o.link || null, actionLabel: o.actionLabel || null,
                    leaving: false, paused: false, duration
                };
                this.items = [...this.items, toast];
                if (this.items.length > 5) this.items = this.items.slice(this.items.length - 5);
                if (duration > 0) {
                    const self = this;
                    const tick = function () {
                        const found = self.items.find(t => t.id === id);
                        if (!found) return;
                        if (found.paused) { setTimeout(tick, 400); return; }
                        self.dismiss(id);
                    };
                    setTimeout(tick, duration);
                }
                return id;
            },
            dismiss(id) {
                this.items = this.items.map(t => t.id === id ? { ...t, leaving: true } : t);
                const self = this;
                setTimeout(() => { self.items = self.items.filter(t => t.id !== id); }, 220);
            },
            success(m, t, o) { return this.add('success', m, t, o); },
            error(m, t, o)   { return this.add('error', m, t, o); },
            warning(m, t, o) { return this.add('warning', m, t, o); },
            info(m, t, o)    { return this.add('info', m, t, o); },
            promo(m, t, o)   { return this.add('promo', m, t, o); }
        });

        /* ── Стор уведомлений: непрочитанные, поллинг, центр ── */
        Alpine.store('notif', {
            unread: 0,
            items: [],
            loading: false,
            open: false,
            muted: false,
            bump: false,
            _lastCheck: 0,
            _shownInitial: false,

            init() {
                try { this.muted = localStorage.getItem('sh-notif-muted') === '1'; } catch (e) {}
                this._shownInitial = sessionStorage.getItem('_notif_initial_shown') === '1';
                this.poll();
                // Скрытая вкладка не опрашивает бэкенд; при возврате — сразу.
                setInterval(() => { if (!document.hidden) this.poll(); }, 15000);
                document.addEventListener('visibilitychange', () => {
                    if (!document.hidden) this.poll();
                });
            },

            toggleMute() {
                this.muted = !this.muted;
                try { localStorage.setItem('sh-notif-muted', this.muted ? '1' : '0'); } catch (e) {}
            },

            async poll() {
                try {
                    const r = await fetch('/api/notifications/unread-count');
                    const d = await r.json();
                    const prev = this.unread;
                    this.unread = d.unread_count || 0;
                    if (this.unread > prev) { this.bump = true; setTimeout(() => this.bump = false, 700); }
                    if (this._lastCheck === 0 && this.unread > 0 && !this._shownInitial) {
                        this._shownInitial = true;
                        sessionStorage.setItem('_notif_initial_shown', '1');
                        Alpine.store('toasts').add('info',
                            'Откройте колокольчик, чтобы посмотреть',
                            'У вас ' + this.unread + ' ' + plural(this.unread,
                                ['непрочитанное уведомление', 'непрочитанных уведомления', 'непрочитанных уведомлений']),
                            { link: '/notifications' });
                    } else if (this._lastCheck > 0 && this.unread > prev) {
                        this.fetchLatestAndToast();
                    }
                    this._lastCheck = Date.now();
                    if (this.open) this.load();
                } catch (e) {}
            },

            async fetchLatestAndToast() {
                try {
                    const r = await fetch('/api/notifications?unread=1&limit=1');
                    const d = await r.json();
                    if (d.items && d.items.length) {
                        const n = d.items[0];
                        if (!this.muted) chime.play(n.category);
                        Alpine.store('toasts').add(n.category, n.message, n.title, { link: n.link });
                    }
                } catch (e) {}
            },

            async load() {
                this.loading = true;
                try {
                    const r = await fetch('/api/notifications?limit=8');
                    const d = await r.json();
                    this.items = d.items || [];
                } catch (e) { this.items = []; }
                this.loading = false;
            },

            toggle() { this.open = !this.open; if (this.open) this.load(); },
            close() { this.open = false; },

            async markRead(id) {
                const it = this.items.find(i => i.id === id);
                if (it && !it.is_read) { it.is_read = true; this.unread = Math.max(0, this.unread - 1); }
                try { await fetch('/api/notifications/' + id + '/read', { method: 'POST' }); } catch (e) {}
            },
            async markAllRead() {
                this.items.forEach(i => i.is_read = true);
                this.unread = 0;
                try { await fetch('/api/notifications/read-all', { method: 'POST' }); } catch (e) {}
            },
            async remove(id) {
                const it = this.items.find(i => i.id === id);
                if (it && !it.is_read) this.unread = Math.max(0, this.unread - 1);
                this.items = this.items.filter(i => i.id !== id);
                try { await fetch('/api/notifications/' + id, { method: 'DELETE' }); } catch (e) {}
            },

            relTime(iso) {
                const d = parseUtc(iso);
                if (!d) return '';
                const diff = Math.max(0, (Date.now() - d.getTime()) / 1000);
                if (diff < 60) return 'только что';
                const m = Math.floor(diff / 60);
                if (m < 60) return m + ' ' + plural(m, ['минуту', 'минуты', 'минут']) + ' назад';
                const h = Math.floor(m / 60);
                if (h < 24) return h + ' ' + plural(h, ['час', 'часа', 'часов']) + ' назад';
                const dd = Math.floor(h / 24);
                if (dd < 7) return dd + ' ' + plural(dd, ['день', 'дня', 'дней']) + ' назад';
                return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
            }
        });

        /* ── Трей фоновых задач: агрегатор /api/tasks/tray ── */
        Alpine.store('tasks', {
            items: [],
            count: 0,
            open: false,
            _timer: null,

            init() {
                this.tick();
                document.addEventListener('visibilitychange', () => {
                    if (!document.hidden) this.tick();
                });
            },

            tick() {
                const self = this;
                if (this._timer) { clearTimeout(this._timer); this._timer = null; }
                // Скрытая вкладка не опрашивает трей — только держит цикл.
                if (document.hidden) {
                    this._timer = setTimeout(function () { self.tick(); }, 30000);
                    return;
                }
                this.poll().then(function () {
                    const delay = (self.count > 0 || self.open) ? 10000 : 30000;
                    if (self._timer) clearTimeout(self._timer);
                    self._timer = setTimeout(function () { self.tick(); }, delay);
                });
            },

            async poll() {
                try {
                    const r = await fetch('/api/tasks/tray');
                    const d = await r.json();
                    this.items = d.items || [];
                    this.count = d.count || 0;
                } catch (e) {}
            },

            toggle() { this.open = !this.open; if (this.open) this.poll(); },
            close() { this.open = false; },
            relTime(iso) {
                const s = Alpine.store('notif');
                return s ? s.relTime(iso) : '';
            }
        });
    });

    /* ─────────────────────────────────────────────
       Прогрессивные улучшения сайдбара:
       aria-current на активной ссылке + title-тултип
       (полезен в свёрнутом рельсе, где виден только значок)
       ───────────────────────────────────────────── */
    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.sidebar-link.active, .sidebar-sublink.active').forEach(function (el) {
            el.setAttribute('aria-current', 'page');
        });
        document.querySelectorAll('.sidebar-link').forEach(function (el) {
            if (el.hasAttribute('title')) return;
            const span = el.querySelector('span');
            const label = span && span.textContent.trim();
            if (label) el.setAttribute('title', label);
        });
    });
})();

/* ── Channel bar: перенос объявленных query-параметров при смене канала ──
   Бар с data-carry="period,..." переписывает href таба при клике, подмешивая
   перечисленные параметры из текущего URL (фильтры не сбрасываются при
   переключении кабинета). Явно заданный в href параметр не перетирается. */
(function () {
    'use strict';
    document.addEventListener('click', function (e) {
        const tab = e.target.closest('.sh-channel-tab');
        if (!tab || !tab.href) return;
        const bar = tab.closest('.sh-channel-bar[data-carry]');
        if (!bar) return;
        try {
            const url = new URL(tab.href, window.location.origin);
            const current = new URLSearchParams(window.location.search);
            bar.dataset.carry.split(',').forEach(function (name) {
                name = name.trim();
                if (!name) return;
                const value = current.get(name);
                if (value !== null && !url.searchParams.has(name)) {
                    url.searchParams.set(name, value);
                }
            });
            tab.href = url.toString();
        } catch (err) { /* не мешаем обычной навигации */ }
    });
})();
