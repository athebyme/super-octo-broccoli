/* Vue owns the connection page; transport/state contract is shared with the no-JS catalogue's Alpine enhancement. */
(function () {
    'use strict';
    const root = document.getElementById('ozon-account-setup');
    if (!root || typeof Vue === 'undefined' || !window.ozonAccountSetup) return;
    let bootstrap;
    try { bootstrap = JSON.parse(root.dataset.ozonSetup || '{}'); } catch (_) { return; }
    const controller = window.ozonAccountSetup();
    const state = {};
    const methods = {};
    for (const [key, value] of Object.entries(controller)) {
        if (typeof value === 'function') methods[key] = value;
        else state[key] = value;
    }
    Vue.createApp({
        data: () => state,
        methods,
        mounted() { this.init(bootstrap); },
        beforeUnmount() { this.destroy(); }
    }).mount(root);
    document.getElementById('ozon-setup-bootstrap-fallback')?.remove();
})();
