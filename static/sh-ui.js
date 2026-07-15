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

            refresh() {
                this.items = Array.from(this.$root.querySelectorAll('.sh-cmdpal-item'));
                this.filter();
            },

            filter() {
                const q = (this.query || '').trim().toLowerCase();
                let firstVisible = -1;
                this.items.forEach((el, i) => {
                    const text = (el.textContent || '').toLowerCase();
                    const match = !q || text.indexOf(q) !== -1;
                    el.style.display = match ? '' : 'none';
                    el.classList.remove('active');
                    if (match && firstVisible === -1) firstVisible = i;
                });
                // Скрыть заголовки групп без видимых пунктов
                this.$root.querySelectorAll('.sh-cmdpal-group').forEach(function (g) {
                    let sib = g.nextElementSibling, anyVisible = false;
                    while (sib && !sib.classList.contains('sh-cmdpal-group')) {
                        if (sib.classList.contains('sh-cmdpal-item') && sib.style.display !== 'none') {
                            anyVisible = true; break;
                        }
                        sib = sib.nextElementSibling;
                    }
                    g.style.display = anyVisible ? '' : 'none';
                });
                this.empty = firstVisible === -1 && this.items.length > 0;
                this.activeIndex = firstVisible;
                this.highlight();
            },

            visibleItems() {
                return this.items.filter(function (el) { return el.style.display !== 'none'; });
            },

            highlight() {
                this.items.forEach(function (el) { el.classList.remove('active'); });
                const el = this.items[this.activeIndex];
                if (el && el.style.display !== 'none') {
                    el.classList.add('active');
                    el.scrollIntoView({ block: 'nearest' });
                }
            },

            move(dir) {
                const vis = this.visibleItems();
                if (!vis.length) return;
                let cur = vis.indexOf(this.items[this.activeIndex]);
                if (cur === -1) cur = dir > 0 ? -1 : 0;
                cur = (cur + dir + vis.length) % vis.length;
                this.activeIndex = this.items.indexOf(vis[cur]);
                this.highlight();
            },

            choose() {
                const el = this.items[this.activeIndex];
                if (el && el.style.display !== 'none') el.click();
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
                setInterval(() => this.poll(), 15000);
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
    });
})();
