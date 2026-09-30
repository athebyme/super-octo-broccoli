/* Presentation only. Filtering, account scope and pagination stay server-owned. */
(function () {
    'use strict';
    window.marketplaceCatalogView = function () {
        return {
            view: 'list',
            init() {
                try {
                    this.view = localStorage.getItem('sh-catalog-view') === 'grid' ? 'grid' : 'list';
                } catch (_) { /* Storage can be unavailable; the list remains usable. */ }
            },
            setView(value) {
                if (value !== 'list' && value !== 'grid') return;
                this.view = value;
                try { localStorage.setItem('sh-catalog-view', value); } catch (_) { /* Optional preference. */ }
            }
        };
    };
})();
