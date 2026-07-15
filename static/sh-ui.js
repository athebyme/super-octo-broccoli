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
})();
