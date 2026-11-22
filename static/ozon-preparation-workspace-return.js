(function (global, document) {
    'use strict';

    if (!global || !document) return;
    var payloadNode = document.getElementById('ozon-prepare-workspace-return');
    var fallbackLink = document.querySelector('a[data-source-prepare-products]');
    var helper = global.SellerHubCommonContent;
    if (!payloadNode || !fallbackLink || !helper ||
        typeof helper.ozonPreparationWorkspaceReturnHref !== 'function') return;

    var payload;
    try {
        payload = JSON.parse(payloadNode.textContent || '');
    } catch (_) {
        return;
    }
    if (!payload || payload.workspaceUrl !== '/my-products/beta') return;

    var href;
    try {
        href = helper.ozonPreparationWorkspaceReturnHref(payload);
    } catch (_) {
        return;
    }
    if (typeof href !== 'string' || !href || href.charAt(0) !== '/' || href.indexOf('//') === 0) return;

    try {
        var target = new URL(href, global.location.origin);
        var params = target.searchParams;
        var keys = Array.from(params.keys());
        var batch = params.get('import_batch');
        var allowedQuery = keys.every(function (key) { return key === 'import_batch'; }) &&
            params.getAll('import_batch').length <= 1 &&
            (batch === null || (batch.length > 0 && batch.length <= 8192 && /^[A-Za-z0-9_.-]+$/.test(batch)));
        if (target.origin !== global.location.origin || target.pathname !== payload.workspaceUrl ||
            target.username || target.password || target.hash === '' ||
            !/^#workspace-return=[a-f0-9]{32}$/.test(target.hash) || !allowedQuery) return;

        fallbackLink.href = target.pathname + target.search + target.hash;
        fallbackLink.setAttribute('aria-label', 'Вернуться в «Мои товары» с прежним списком');
        fallbackLink.title = 'Открыть прежний список и восстановить отмеченные товары';
        fallbackLink.dataset.workspaceReturn = 'verified';
    } catch (_) {
        /* An invalid capsule leaves the ordinary fallback link untouched. */
    }
})(typeof window !== 'undefined' ? window : null,
   typeof document !== 'undefined' ? document : null);
