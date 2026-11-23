/* Мои товары · beta — экран массовых операций.
   Правило страницы: выбрал строки → видишь, что произойдёт → подтвердил →
   следишь за прогрессом → получаешь честный итог. */
(function () {
    'use strict';

    if (typeof window.shVue === 'undefined') return;
    var boot = window.shVue.bootstrapData('mp-bootstrap');
    var importBundle = window.SellerHubMyProductsImport || null;
    var importPanelAvailable = !!(importBundle && importBundle.SupplierImportPanel &&
        typeof importBundle.canonicalBatchLocation === 'function');
    var priceWorkspaceUrls = boot.urls || {};
    var priceWorkspaceDialogAvailable = !!(importBundle && importBundle.WorkspacePriceDialog &&
        ['wbPreview', 'wbSave', 'ozonPreview', 'ozonSave'].every(function (key) {
            return typeof priceWorkspaceUrls[key] === 'string' &&
                priceWorkspaceUrls[key].charAt(0) === '/';
        }));
    if (importPanelAvailable) {
        window.shVue.components['mp-supplier-import'] = importBundle.SupplierImportPanel;
    }
    if (priceWorkspaceDialogAvailable) {
        window.shVue.components['workspace-price-dialog'] = importBundle.WorkspacePriceDialog;
    }
    var importBatchQueryRequested = false;
    var locationSearch = window.location && typeof window.location.search === 'string'
        ? window.location.search : '';
    try {
        importBatchQueryRequested = new URLSearchParams(locationSearch).has('import_batch');
    } catch (_) {
        importBatchQueryRequested = /(?:^|[?&])import_batch(?:=|&|$)/.test(locationSearch);
    }
    var hasImportBatchToken = typeof boot.importBatch === 'string';
    var suppliedBatchKind = boot.batchKind;
    var importBatchMode = hasImportBatchToken || importBatchQueryRequested ||
        (suppliedBatchKind !== undefined && suppliedBatchKind !== null);
    var importBatchToken = hasImportBatchToken ? boot.importBatch : '';
    var importBatchKind = suppliedBatchKind === undefined || suppliedBatchKind === null
        ? 'import' : suppliedBatchKind;
    var importBatchKindInvalid = importBatchMode &&
        !['import', 'supplier_refresh_result'].includes(importBatchKind);
    var supplierRefreshResultBatchMode = importBatchMode &&
        importBatchKind === 'supplier_refresh_result';
    var importBatchInvalid = importBatchMode && (
        !hasImportBatchToken || !importBatchToken || importBatchToken.length > 8192 || importBatchKindInvalid
    );
    var supplierRefreshBatchQueryRequested = false;
    try {
        supplierRefreshBatchQueryRequested = new URLSearchParams(locationSearch).has('supplier_refresh_batch');
    } catch (_) {
        supplierRefreshBatchQueryRequested = /(?:^|[?&])supplier_refresh_batch(?:=|&|$)/.test(locationSearch);
    }
    var hasSupplierRefreshBatchToken = typeof boot.supplierRefreshBatch === 'string';
    var supplierRefreshBatchMode = hasSupplierRefreshBatchToken || supplierRefreshBatchQueryRequested;
    var supplierRefreshBatchToken = hasSupplierRefreshBatchToken ? boot.supplierRefreshBatch : '';
    var supplierRefreshBatchInvalid = supplierRefreshBatchMode && (
        boot.supplierRefreshBatchInvalid === true || !hasSupplierRefreshBatchToken ||
        !supplierRefreshBatchToken || supplierRefreshBatchToken.length > 8192
    );
    var supplierRefreshReceiptKey = 'supplier-refresh-resume-v1';
    var MAX_SUPPLIER_REFRESH_RECEIPT = 7500;
    function validSupplierRefreshReceipt(value) {
        return typeof value === 'string' && value.length > 0 &&
            value.length <= MAX_SUPPLIER_REFRESH_RECEIPT && /^[A-Za-z0-9_.-]+$/.test(value);
    }
    function readSupplierRefreshReceipt() {
        try {
            var value = sessionStorage.getItem(supplierRefreshReceiptKey);
            if (value === null) return { receipt: null, storageError: false };
            if (!validSupplierRefreshReceipt(value)) {
                sessionStorage.removeItem(supplierRefreshReceiptKey);
                return { receipt: null, storageError: false };
            }
            return { receipt: value, storageError: false };
        } catch (_) {
            return { receipt: null, storageError: true };
        }
    }
    function saveSupplierRefreshReceipt(value) {
        if (!validSupplierRefreshReceipt(value)) return false;
        try {
            sessionStorage.setItem(supplierRefreshReceiptKey, value);
            return true;
        } catch (_) {
            return false;
        }
    }
    function clearSupplierRefreshReceipt(expectedValue) {
        try {
            if (expectedValue === undefined ||
                sessionStorage.getItem(supplierRefreshReceiptKey) === expectedValue) {
                sessionStorage.removeItem(supplierRefreshReceiptKey);
            }
        } catch (_) {}
    }
    var ozonPendingKey = 'ozon-source-prepare-v1';
    var ozonInlineReceiptKey = 'seller-hub-ozon-import-prepare-receipt-v1';
    var SIGNED_IMPORT_BATCH = /^[A-Za-z0-9_.-]{1,8192}$/;
    function validInlineOzonContext(value, productIds) {
        if (!value || typeof value !== 'object' || Array.isArray(value) ||
            value.owner !== boot.workspaceOwner ||
            typeof value.import_batch !== 'string' || !SIGNED_IMPORT_BATCH.test(value.import_batch) ||
            !Array.isArray(value.selection_ids) || !sameOrderedIds(value.selection_ids, productIds) ||
            !Array.isArray(value.product_labels) || value.product_labels.length !== productIds.length) return false;
        return value.product_labels.every(function (row, index) {
            return row && typeof row === 'object' && !Array.isArray(row) &&
                row.id === productIds[index] && typeof row.label === 'string' &&
                row.label.length > 0 && row.label.length <= 180;
        });
    }
    function validOzonInlineReceipt(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value) || value.version !== 1 ||
            value.owner !== boot.workspaceOwner ||
            typeof value.import_batch !== 'string' || !SIGNED_IMPORT_BATCH.test(value.import_batch) ||
            !Array.isArray(value.latest && value.latest.product_ids) || !value.latest.product_ids.length ||
            value.latest.product_ids.length > MAX_BULK_SELECTION ||
            value.latest.product_ids.some(function (id) { return !Number.isSafeInteger(id) || id <= 0; }) ||
            new Set(value.latest.product_ids).size !== value.latest.product_ids.length) return false;
        var latest = value.latest;
        return !!latest && Number.isSafeInteger(latest.account_id) && latest.account_id > 0 &&
            typeof latest.account_label === 'string' && latest.account_label.length <= 120 &&
            /^ozon-upload-[0-9a-f]{32}$/.test(latest.job_uid) &&
            Array.isArray(latest.product_labels) &&
            latest.product_labels.length === latest.product_ids.length &&
            latest.product_labels.every(function (row, index) {
                return row && row.id === latest.product_ids[index] &&
                    typeof row.label === 'string' && row.label.length > 0 && row.label.length <= 180;
            });
    }
    function readOzonInlineReceipt() {
        try {
            var raw = sessionStorage.getItem(ozonInlineReceiptKey);
            if (raw === null) return { receipt: null, storageError: false };
            var value = JSON.parse(raw);
            if (!validOzonInlineReceipt(value)) {
                sessionStorage.removeItem(ozonInlineReceiptKey);
                return { receipt: null, storageError: false };
            }
            return { receipt: value, storageError: false };
        } catch (_) {
            return { receipt: null, storageError: true };
        }
    }
    function saveOzonInlineReceipt(value) {
        if (!validOzonInlineReceipt(value)) return false;
        try { sessionStorage.setItem(ozonInlineReceiptKey, JSON.stringify(value)); return true; }
        catch (_) { return false; }
    }
    function inlineImportMarkerToken() {
        var marker = window.history && window.history.state && window.history.state.sellerHubInlineImportBatch;
        return importBatchMode && importBatchKind === 'import' && marker && marker.v === 1 &&
            marker.owner === boot.workspaceOwner && marker.token === importBatchToken &&
            typeof marker.token === 'string' && SIGNED_IMPORT_BATCH.test(marker.token)
            ? marker.token : '';
    }
    function ozonRunLocation(base, jobUid) {
        if (typeof base !== 'string' || base.length > 2000 ||
            !/^ozon-upload-[0-9a-f]{32}$/.test(jobUid)) return '';
        try {
            var target = new URL(base, window.location.origin);
            if (target.origin !== window.location.origin || target.username || target.password ||
                target.search || target.hash || !target.pathname.endsWith('/')) return '';
            return target.pathname + jobUid;
        } catch (_) { return ''; }
    }
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
            new Set(value.imported_product_ids).size === value.imported_product_ids.length &&
            (value.inline_import_context === undefined ||
                validInlineOzonContext(value.inline_import_context, value.imported_product_ids));
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

    function trackedJobForUi(data, expectedUid) {
        if (!data || typeof data !== 'object') return null;
        var raw = data.job && typeof data.job === 'object' ? data.job : data;
        if (!raw || typeof raw.status !== 'string' || (raw.job_uid && raw.job_uid !== expectedUid)) return null;
        var job = Object.assign({}, raw);
        job.job_uid = expectedUid;
        if (job.failed === undefined && job.failed_count !== undefined) job.failed = job.failed_count;
        if (job.status === 'completed') job.status = 'done';
        if (job.status === 'failed') job.message = 'Задача не завершена.';
        if (job.status === 'cancelled') job.message = 'Операция отменена.';
        if (job.status === 'unknown') {
            job.message = 'Проверьте статус этого запуска перед новой отправкой.';
        }
        return job;
    }

    var MAX_BULK_SELECTION = 200;
    var MAX_OZON_PRICE_HANDOFF = 100;
    var MAX_WORKSPACE_UPDATE_TOKEN_LENGTH = 32 * 1024;
    var SUPPLIER_REFRESH_PREVIEW_TIMEOUT_MS = 12000;
    var WB_CREATE_PENDING_KEY = 'seller-hub-wb-create-pending-v1';
    var WB_CREATE_PENDING_MAX_BYTES = 24 * 1024;
    var WB_CREATE_STATUSES = ['pending', 'running', 'unknown', 'completed', 'failed', 'cancelled'];
    var WB_CREATE_JOB_UID_RE = /^bulk_wb_[0-9a-f]{56}$/;
    var WB_CREATE_NOT_ACCEPTED_CODES = [
        'wb_create_invalid_request', 'wb_create_invalid_scope',
        'wb_create_request_key_invalid', 'wb_create_owner_scope_changed',
        'wb_create_wb_account_unavailable', 'wb_create_scope_mismatch',
        'wb_create_overlap',
    ];
    function wbCreateStorageKey() {
        return WB_CREATE_PENDING_KEY + ':' + encodeURIComponent(
            typeof boot.workspaceOwner === 'string' ? boot.workspaceOwner : '',
        );
    }
    var WORKSPACE_RETURN_SCOPE_KEYS = [
        'activeTab', 'search', 'supplier', 'has_photos', 'stock', 'sort', 'visibleCount',
    ];
    var WORKSPACE_RETURN_SCOPE_OPTIONAL_KEYS = ['marketplace', 'marketplace_state', 'catalog_category'];
    var CATALOG_CATEGORY_MORE_OPTION = '__load_more_catalog_categories__';
    function validWbCreateIds(ids, allowEmpty) {
        return Array.isArray(ids) && (allowEmpty || ids.length > 0) && ids.length <= MAX_BULK_SELECTION &&
            ids.every(function (id) { return Number.isSafeInteger(id) && id > 0; }) &&
            new Set(ids).size === ids.length;
    }

    function validCatalogCategoryToken(value) {
        if (value === 'missing') return true;
        if (typeof value !== 'string' || value.slice(0, 6) !== 'label:') return false;
        var label = value.slice(6);
        var codePoints = Array.from(label);
        for (var index = 0; index < label.length; index += 1) {
            var codeUnit = label.charCodeAt(index);
            if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
                var lowSurrogate = label.charCodeAt(index + 1);
                if (lowSurrogate < 0xdc00 || lowSurrogate > 0xdfff) return false;
                index += 1;
            } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
                return false;
            }
        }
        return codePoints.length >= 1 && codePoints.length <= 200 &&
            !/[\u0000-\u001f\u007f-\u009f]/.test(label);
    }

    function validCatalogCategoryOption(option) {
        return !!option && typeof option === 'object' && !Array.isArray(option) &&
            Object.keys(option).length === 2 &&
            Object.prototype.hasOwnProperty.call(option, 'key') &&
            Object.prototype.hasOwnProperty.call(option, 'label') &&
            typeof option.key === 'string' && typeof option.label === 'string' &&
            option.key === 'label:' + option.label && validCatalogCategoryToken(option.key);
    }

    function validCatalogCategoryPage(data, requestedPage) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || data.success !== true ||
            data.page !== requestedPage || !Array.isArray(data.options) ||
            data.options.length > 100 || typeof data.has_next !== 'boolean' ||
            typeof data.has_uncategorized !== 'boolean') return false;
        var seen = new Set();
        for (var index = 0; index < data.options.length; index += 1) {
            var option = data.options[index];
            if (!validCatalogCategoryOption(option) || seen.has(option.key)) return false;
            seen.add(option.key);
        }
        return true;
    }

    function validWbCreatePending(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value) || value.version !== 1 ||
            typeof boot.workspaceOwner !== 'string' || !boot.workspaceOwner ||
            value.owner !== boot.workspaceOwner || typeof value.import_batch !== 'string' ||
            !SIGNED_IMPORT_BATCH.test(value.import_batch) ||
            typeof value.request_key !== 'string' || !/^[a-f0-9]{32}$/.test(value.request_key) ||
            !validWbCreateIds(value.selection_ids, false) ||
            !validWbCreateIds(value.product_ids, false) ||
            value.product_ids.some(function (id) { return value.selection_ids.indexOf(id) === -1; }) ||
            !['posting', 'unknown', 'accepted', 'terminal_refresh'].includes(value.state) ||
            typeof value.selection_changed !== 'boolean' ||
            !validWbCreateIds(value.current_selection_ids, true) ||
            (value.job_uid !== null && (typeof value.job_uid !== 'string' ||
                !WB_CREATE_JOB_UID_RE.test(value.job_uid))) ||
            (value.receipt_status !== null && !WB_CREATE_STATUSES.includes(value.receipt_status)) ||
            (value.worker_exited !== null && typeof value.worker_exited !== 'boolean') ||
            !Number.isSafeInteger(value.created_at) || value.created_at <= 0) return false;
        var terminal = ['completed', 'failed', 'cancelled'].includes(value.receipt_status);
        if (value.state === 'posting') {
            return value.job_uid === null && value.receipt_status === null && value.worker_exited === false;
        }
        if (value.state === 'accepted') {
            return !!value.job_uid && ['pending', 'running'].includes(value.receipt_status) &&
                value.worker_exited === false;
        }
        if (value.state === 'terminal_refresh') {
            return !!value.job_uid && terminal && value.worker_exited === true;
        }
        return value.worker_exited === false &&
            (value.receipt_status === null || value.receipt_status === 'unknown');
    }

    function readWbCreatePending() {
        try {
            var raw = sessionStorage.getItem(wbCreateStorageKey());
            if (raw === null) return { pending: null, storageProblem: false };
            if (raw.length > WB_CREATE_PENDING_MAX_BYTES) return { pending: null, storageProblem: true };
            var value = JSON.parse(raw);
            if (!validWbCreatePending(value)) return { pending: null, storageProblem: true };
            return { pending: value, storageProblem: false };
        } catch (_) {
            return { pending: null, storageProblem: true };
        }
    }

    function saveWbCreatePending(value) {
        if (!validWbCreatePending(value)) return false;
        try {
            var encoded = JSON.stringify(value);
            if (encoded.length > WB_CREATE_PENDING_MAX_BYTES) return false;
            sessionStorage.setItem(wbCreateStorageKey(), encoded);
            return true;
        } catch (_) {
            return false;
        }
    }

    function clearWbCreatePending(requestKey) {
        try {
            var raw = sessionStorage.getItem(wbCreateStorageKey());
            var value = raw ? JSON.parse(raw) : null;
            if (value && value.request_key === requestKey) sessionStorage.removeItem(wbCreateStorageKey());
        } catch (_) {}
    }

    function wbCreateRequestKey() {
        var cryptoApi = window.crypto;
        if (!cryptoApi || typeof cryptoApi.getRandomValues !== 'function') return '';
        try {
            if (typeof cryptoApi.randomUUID === 'function') {
                return cryptoApi.randomUUID().replace(/-/g, '').toLowerCase();
            }
            return Array.from(cryptoApi.getRandomValues(new Uint8Array(16)), function (byte) {
                return byte.toString(16).padStart(2, '0');
            }).join('');
        } catch (_) { return ''; }
    }

    function validWbCreateReceipt(data, pending) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || data.success !== true ||
            typeof data.job_uid !== 'string' || !WB_CREATE_JOB_UID_RE.test(data.job_uid) ||
            (pending.job_uid && data.job_uid !== pending.job_uid) ||
            !Number.isSafeInteger(data.total) || data.total !== pending.product_ids.length ||
            !sameOrderedIds(data.product_ids, pending.product_ids) ||
            !WB_CREATE_STATUSES.includes(data.status) || typeof data.replayed !== 'boolean' ||
            typeof data.worker_exited !== 'boolean') return false;
        var terminalStatus = ['completed', 'failed', 'cancelled'].includes(data.status);
        return terminalStatus ? data.worker_exited === true : data.worker_exited === false;
    }

    function wbCreateReceiptCounts(data, total) {
        var counts = data && data.result_counts;
        if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return null;
        var processed = counts.processed;
        var succeeded = counts.succeeded;
        var failed = counts.failed;
        if (![processed, succeeded, failed].every(function (value) {
            return Number.isSafeInteger(value) && value >= 0;
        }) || processed > total || succeeded > processed || failed > processed ||
            succeeded + failed > processed) return null;
        return { processed: processed, succeeded: succeeded, failed: failed };
    }

    function knownWbCreateNotAccepted(response, data) {
        return !!response && response.status >= 400 && response.status < 500 &&
            !!data && typeof data === 'object' && !Array.isArray(data) &&
            data.success === false && data.accepted === false &&
            typeof data.code === 'string' && WB_CREATE_NOT_ACCEPTED_CODES.includes(data.code);
    }
    function exactWbProductIds(items) {
        if (!Array.isArray(items) || !items.length) return null;
        var ids = items.map(function (item) {
            return item && item.wb ? item.wb.product_id : null;
        });
        if (ids.some(function (id) {
            return !Number.isSafeInteger(id) || id <= 0;
        }) || new Set(ids).size !== ids.length) return null;
        return ids;
    }
    function sameIds(left, right) {
        if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
        if (new Set(left).size !== left.length || new Set(right).size !== right.length) return false;
        var rightSet = new Set(right);
        return left.every(function (id) { return rightSet.has(id); });
    }

    function sameOrderedIds(left, right) {
        return Array.isArray(left) && Array.isArray(right) && left.length === right.length &&
            left.every(function (id, index) { return id === right[index]; });
    }

    var SELECTION_REVIEW_RETURN_KEY = 'my-products-selection-review-return-v1';
    function validSelectionReviewIds(ids) {
        return Array.isArray(ids) && ids.length > 0 && ids.length <= MAX_BULK_SELECTION &&
            ids.every(function (id) { return Number.isSafeInteger(id) && id > 0; }) &&
            new Set(ids).size === ids.length;
    }
    function saveSelectionReviewReturn(app, nonce, selectionIds) {
        if (!app || app.importBatchMode || app.supplierRefreshBatchMode ||
            !workspaceReturnHash(nonce) || !validSelectionReviewIds(selectionIds) ||
            !app.selectionRowsVerified ||
            (!app.selectionReviewActive && !app.selectedOutsideCurrentListCount)) return false;
        try {
            sessionStorage.setItem(SELECTION_REVIEW_RETURN_KEY, JSON.stringify({
                v: 1,
                owner: boot.workspaceOwner,
                nonce: nonce,
                ids: selectionIds.slice(),
            }));
            return true;
        } catch (_) { return false; }
    }
    function readSelectionReviewReturn(capsule) {
        try {
            var raw = sessionStorage.getItem(SELECTION_REVIEW_RETURN_KEY);
            if (raw === null) return null;
            var value = JSON.parse(raw);
            if (!value || typeof value !== 'object' || Array.isArray(value) || value.v !== 1 ||
                value.owner !== boot.workspaceOwner || !capsule || value.nonce !== capsule.nonce ||
                !validSelectionReviewIds(value.ids) || !sameOrderedIds(value.ids, capsule.selectionIds) ||
                capsule.batch !== null || importBatchMode || supplierRefreshBatchMode) {
                sessionStorage.removeItem(SELECTION_REVIEW_RETURN_KEY);
                return null;
            }
            return value;
        } catch (_) { return null; }
    }
    function clearSelectionReviewReturn(nonce) {
        try {
            var raw = sessionStorage.getItem(SELECTION_REVIEW_RETURN_KEY);
            if (raw === null) return;
            var value = JSON.parse(raw);
            if (nonce === undefined || !value || value.nonce === nonce) {
                sessionStorage.removeItem(SELECTION_REVIEW_RETURN_KEY);
            }
        } catch (_) {}
    }

    function normalizedWbAccountId(value) {
        if (value === null) return null;
        if (typeof value !== 'string') return undefined;
        var normalized = value.trim();
        return normalized || null;
    }

    var wbAccountIdFromBootstrap = Object.prototype.hasOwnProperty.call(boot, 'wbAccountId')
        ? normalizedWbAccountId(boot.wbAccountId)
        : undefined;

    function orderedWbProductIds(items, selectionIds) {
        if (!Array.isArray(items) || !Array.isArray(selectionIds) ||
            !selectionIds.length || selectionIds.length > MAX_BULK_SELECTION ||
            new Set(selectionIds).size !== selectionIds.length) return null;
        var byId = new Map();
        for (var index = 0; index < items.length; index += 1) {
            var item = items[index];
            if (!item || !Number.isSafeInteger(item.id) || item.id <= 0 || byId.has(item.id)) return null;
            byId.set(item.id, item);
        }
        if (byId.size < selectionIds.length ||
            selectionIds.some(function (id) { return !byId.has(id); })) return null;
        return exactWbProductIds(selectionIds.map(function (id) { return byId.get(id); }));
    }

    function validWbWorkspaceSelectionResponse(data, expectedProductIds) {
        return !!data && typeof data.selection_token === 'string' &&
            data.selection_token.length > 0 && data.selection_token.length <= 24 * 1024 &&
            data.marketplace === 'wb' && data.count === expectedProductIds.length &&
            sameOrderedIds(data.ids, expectedProductIds);
    }

    function storePriceWorkspaceReturn(app, priceTarget, selectionIds, scope) {
        if (!scope || !Array.isArray(selectionIds)) return null;
        var helpers = window.SellerHubCommonContent || {};
        if (typeof helpers.storeWorkspaceReturn !== 'function') return null;
        try {
            var nonce = helpers.storeWorkspaceReturn({
                owner: boot.workspaceOwner,
                priceTarget: priceTarget,
                selectionIds: selectionIds.slice(),
                scope: scope,
                batch: app.importBatchMode ? app.importBatch : null,
            });
            saveSelectionReviewReturn(app, nonce, selectionIds);
            return nonce;
        } catch (_) {
            return null;
        }
    }

    function storeOzonPreparationWorkspaceReturn(app, accountId, productIds, selectionIds, scope) {
        if (!scope || !Array.isArray(productIds) || !Array.isArray(selectionIds)) return null;
        var helpers = window.SellerHubCommonContent || {};
        if (typeof helpers.storeWorkspaceReturn !== 'function') return null;
        try {
            var nonce = helpers.storeWorkspaceReturn({
                owner: boot.workspaceOwner,
                prepareTarget: {
                    accountId: accountId,
                    productIds: productIds.slice(),
                },
                selectionIds: selectionIds.slice(),
                scope: scope,
                batch: app.importBatchMode ? app.importBatch : null,
            });
            saveSelectionReviewReturn(app, nonce, selectionIds);
            return workspaceReturnHash(nonce) ? nonce : null;
        } catch (_) {
            return null;
        }
    }

    function storeWbEditWorkspaceReturn(app, productIds, selectionIds, scope) {
        if (!scope || wbAccountIdFromBootstrap === undefined ||
            !Array.isArray(productIds) || !Array.isArray(selectionIds) ||
            productIds.length !== selectionIds.length) return null;
        var helpers = window.SellerHubCommonContent || {};
        if (typeof helpers.storeWorkspaceReturn !== 'function') return null;
        try {
            var nonce = helpers.storeWorkspaceReturn({
                owner: boot.workspaceOwner,
                wbEditTarget: {
                    accountId: wbAccountIdFromBootstrap,
                    productIds: productIds.slice(),
                },
                selectionIds: selectionIds.slice(),
                scope: scope,
                batch: app.importBatchMode ? app.importBatch : null,
            });
            saveSelectionReviewReturn(app, nonce, selectionIds);
            return nonce;
        } catch (_) {
            return null;
        }
    }

    function storeOzonWorkspaceUpdateReturn(app, accountId, listingIds, selectionIds, scope) {
        if (!scope || !Number.isSafeInteger(accountId) || accountId <= 0 ||
            !Array.isArray(listingIds) || !Array.isArray(selectionIds) ||
            listingIds.length !== selectionIds.length || !listingIds.length ||
            listingIds.length > MAX_BULK_SELECTION) return null;
        var helpers = window.SellerHubCommonContent || {};
        if (typeof helpers.storeWorkspaceReturn !== 'function') return null;
        try {
            var nonce = helpers.storeWorkspaceReturn({
                owner: boot.workspaceOwner,
                ozonUpdateTarget: {
                    accountId: accountId,
                    listingIds: listingIds.slice(),
                },
                selectionIds: selectionIds.slice(),
                scope: scope,
                batch: app.importBatchMode ? app.importBatch : null,
            });
            saveSelectionReviewReturn(app, nonce, selectionIds);
            return workspaceReturnHash(nonce) ? nonce : null;
        } catch (_) {
            return null;
        }
    }

    function bindOzonPreparationWorkspaceReturn(pending, jobUid) {
        var hash = workspaceReturnHash(pending && pending.workspace_return_nonce);
        var helpers = window.SellerHubCommonContent || {};
        if (!hash || typeof helpers.bindOzonPreparationWorkspaceResult !== 'function' ||
            !pending || !Array.isArray(pending.imported_product_ids)) return false;
        try {
            var bound = helpers.bindOzonPreparationWorkspaceResult(hash, {
                owner: boot.workspaceOwner,
                prepareTarget: {
                    accountId: pending.account_id,
                    productIds: pending.imported_product_ids.slice(),
                },
                jobUid: jobUid,
            });
            return bound === hash;
        } catch (_) {
            return false;
        }
    }

    function workspaceReturnHash(nonce) {
        return typeof nonce === 'string' && /^[a-f0-9]{32}$/.test(nonce)
            ? '#workspace-return=' + nonce
            : '';
    }

    function wbWorkspaceReturnUrl(app, nonce) {
        if (!app || (nonce !== null && nonce !== undefined && !workspaceReturnHash(nonce))) return '';
        var query = new URLSearchParams();
        if (app.importBatchMode) {
            if (!validSupplierUpdatedBatchToken(app.importBatch)) return '';
            query.set('import_batch', app.importBatch);
        }
        return '/my-products/beta' + (query.toString() ? '?' + query.toString() : '') +
            (nonce ? workspaceReturnHash(nonce) : '');
    }

    function orderedProductIds(items) {
        if (!Array.isArray(items)) return null;
        var ids = [];
        var seen = new Set();
        for (var i = 0; i < items.length; i += 1) {
            var id = items[i] && items[i].id;
            if (!Number.isSafeInteger(id) || id <= 0 || seen.has(id)) return null;
            seen.add(id);
            ids.push(id);
        }
        return ids;
    }

    function validSupplierUpdatedBatchToken(value) {
        return typeof value === 'string' && value.length > 0 && value.length <= 8192;
    }

    function exactWorkspaceFeedPage(data, perPage) {
        var pagination = data && data.pagination;
        var ids = data && orderedProductIds(data.items);
        if (!data || data.success !== true || !Array.isArray(data.items) || !ids ||
            !pagination || pagination.page !== 1 || pagination.per_page !== perPage ||
            !Number.isSafeInteger(pagination.total) || pagination.total < 0 ||
            pagination.pages !== Math.ceil(pagination.total / perPage) ||
            pagination.has_next !== (pagination.total > perPage) ||
            data.items.length !== Math.min(pagination.total, perPage)) return null;
        return { ids: ids, pagination: pagination };
    }

    function exactImportBatchPage(data, expectedKind) {
        var pagination = data && data.pagination;
        var count = data && data.import_batch_count;
        var ids = data && orderedProductIds(data.items);
        var expected = expectedKind || 'import';
        var kindMatches = expected === 'supplier_refresh_result'
            ? data && data.batch_kind === expected
            : data && (data.batch_kind === expected || data.batch_kind === undefined);
        return !!data && data.success === true &&
            kindMatches &&
            Number.isSafeInteger(count) && count >= 1 &&
            count <= MAX_BULK_SELECTION && Array.isArray(data.items) &&
            data.items.length === count && ids &&
            pagination && pagination.page === 1 &&
            pagination.per_page === MAX_BULK_SELECTION &&
            pagination.total === count && pagination.pages === 1 &&
            pagination.has_next === false;
    }

    function exactSupplierRefreshBatchPage(data) {
        var pagination = data && data.pagination;
        var count = data && data.supplier_refresh_batch_count;
        var ids = data && orderedProductIds(data.items);
        return !!data && data.success === true &&
            Number.isSafeInteger(count) && count >= 1 &&
            count <= MAX_BULK_SELECTION && Array.isArray(data.items) &&
            data.items.length === count && ids &&
            pagination && pagination.page === 1 &&
            pagination.per_page === MAX_BULK_SELECTION &&
            pagination.total === count && pagination.pages === 1 &&
            pagination.has_next === false;
    }

    function exactOzonListingAccountChoices(items, selectionIds, accounts, maximum) {
        var maxSelection = Number.isSafeInteger(maximum) && maximum > 0
            ? maximum : MAX_OZON_PRICE_HANDOFF;
        if (!Array.isArray(selectionIds) || !selectionIds.length ||
            selectionIds.length > maxSelection) {
            return {
                choices: [],
                reason: Array.isArray(selectionIds) && selectionIds.length > maxSelection
                    ? 'limit' : 'selection',
            };
        }
        if (selectionIds.some(function (id) {
            return !Number.isSafeInteger(id) || id <= 0;
        }) || new Set(selectionIds).size !== selectionIds.length ||
            !Array.isArray(items) || items.length !== selectionIds.length) {
            return { choices: [], reason: 'selection' };
        }

        var itemsById = new Map();
        for (var i = 0; i < items.length; i += 1) {
            var item = items[i];
            if (!item || !Number.isSafeInteger(item.id) || item.id <= 0 ||
                itemsById.has(item.id)) return { choices: [], reason: 'selection' };
            itemsById.set(item.id, item);
        }
        if (!sameIds(selectionIds, Array.from(itemsById.keys()))) {
            return { choices: [], reason: 'selection' };
        }

        if (!Array.isArray(accounts)) return { choices: [], reason: 'accounts' };
        var accountIds = new Set();
        var activeAccounts = [];
        for (var a = 0; a < accounts.length; a += 1) {
            var account = accounts[a];
            if (!account || !Number.isSafeInteger(account.id) || account.id <= 0 ||
                accountIds.has(account.id)) return { choices: [], reason: 'accounts' };
            accountIds.add(account.id);
            // The page bootstrap is already seller/Ozon/active scoped. Honor
            // explicit status fields too when a newer DTO supplies them.
            if ((account.is_active !== undefined && account.is_active !== true) ||
                (account.marketplace_code !== undefined &&
                    account.marketplace_code !== 'ozon') ||
                (account.connection_status !== undefined &&
                    account.connection_status !== 'connected')) continue;
            activeAccounts.push(account);
        }
        if (!activeAccounts.length) return { choices: [], reason: 'no_accounts' };

        var choices = [];
        activeAccounts.forEach(function (account) {
            var listingIds = [];
            var eligible = true;
            selectionIds.forEach(function (productId) {
                if (!eligible) return;
                var product = itemsById.get(productId);
                var listings = product && product.ozon && product.ozon.listings;
                if (!Array.isArray(listings)) {
                    eligible = false;
                    return;
                }
                var accountRows = listings.filter(function (listing) {
                    return listing && listing.account_id === account.id;
                });
                if (accountRows.length !== 1) {
                    eligible = false;
                    return;
                }
                var listing = accountRows[0];
                if (listing.marketplace_code !== 'ozon' ||
                    listing.link_status !== 'linked' ||
                    listing.account_is_active !== true ||
                    listing.is_available !== true ||
                    listing.is_archived !== false ||
                    !Number.isSafeInteger(listing.id) || listing.id <= 0) {
                    eligible = false;
                    return;
                }
                listingIds.push(listing.id);
            });
            if (eligible && listingIds.length === selectionIds.length &&
                new Set(listingIds).size === listingIds.length) {
                choices.push({
                    account_id: account.id,
                    account_label: typeof account.label === 'string' ? account.label : '',
                    listing_ids: listingIds,
                });
            }
        });
        return {
            choices: choices,
            reason: choices.length ? null : 'no_common_account',
        };
    }

    function exactOzonPriceAccountChoices(items, selectionIds, accounts) {
        return exactOzonListingAccountChoices(
            items, selectionIds, accounts, MAX_OZON_PRICE_HANDOFF,
        );
    }

    function exactOzonWorkspaceUpdateAccountChoices(items, selectionIds, accounts) {
        return exactOzonListingAccountChoices(
            items, selectionIds, accounts, MAX_BULK_SELECTION,
        );
    }

    function validOzonWorkspaceUpdateHandoffUrl(value, accountId) {
        if (typeof value !== 'string' || !value || value.length > 32 * 1024 ||
            value.charAt(0) !== '/' || value.indexOf('//') === 0 ||
            /[\u0000-\u001f\u007f]/.test(value)) return '';
        try {
            var target = new URL(value, window.location.origin);
            if (target.origin !== window.location.origin || target.username || target.password ||
                target.pathname !== '/marketplaces/listings/') return '';
            var keys = Array.from(target.searchParams.keys()).sort();
            if (keys.length !== 3 || keys[0] !== 'account_id' ||
                keys[1] !== 'marketplace' || keys[2] !== 'workspace_update') return '';
            var accountValues = target.searchParams.getAll('account_id');
            var marketplaceValues = target.searchParams.getAll('marketplace');
            var tokenValues = target.searchParams.getAll('workspace_update');
            if (accountValues.length !== 1 || marketplaceValues.length !== 1 ||
                tokenValues.length !== 1 || accountValues[0] !== String(accountId) ||
                marketplaceValues[0] !== 'ozon' || tokenValues[0] !== '1' ||
                target.search !== '?marketplace=ozon&account_id=' + accountId + '&workspace_update=1') return '';
            var fragment = target.hash.match(/^#workspace-update=([A-Za-z0-9_.-]+)$/);
            if (!fragment || fragment[1].length > MAX_WORKSPACE_UPDATE_TOKEN_LENGTH ||
                value !== target.pathname + target.search + target.hash) return '';
            return target.pathname + target.search + target.hash;
        } catch (_) {
            return '';
        }
    }

    function labelProduct(item) {
        var title = item && typeof item.title === 'string' ? item.title.trim() : '';
        var vendorCode = item && typeof item.vendor_code === 'string' ? item.vendor_code.trim() : '';
        if (title && vendorCode && title !== vendorCode) return title + ' · ' + vendorCode;
        return title || vendorCode || 'Товар из выбранного списка';
    }

    function selectedProductLabels(items, ids) {
        var byId = new Map((items || []).map(function (item) { return [item.id, item]; }));
        return ids.map(function (id) {
            return { productId: id, label: labelProduct(byId.get(id)) };
        });
    }

    function nonnegativeCount(value) {
        return Number.isSafeInteger(value) && value >= 0;
    }

    function exactSupplierRefreshWbProductIds(data, updatedIds) {
        if (!data || !nonnegativeCount(data.wb_ready) ||
            data.wb_ready > MAX_BULK_SELECTION ||
            !Array.isArray(data.wb_targets) ||
            data.wb_targets.length !== data.wb_ready ||
            data.wb_targets.length > updatedIds.length) return null;

        var updatedIndexById = new Map(updatedIds.map(function (id, index) {
            return [id, index];
        }));
        var seenWbProductIds = new Set();
        var lastUpdatedIndex = -1;
        var productIds = [];

        for (var i = 0; i < data.wb_targets.length; i += 1) {
            var target = data.wb_targets[i];
            if (!target || typeof target !== 'object' || Array.isArray(target)) return null;
            var keys = Object.keys(target).sort();
            if (keys.length !== 2 || keys[0] !== 'imported_product_id' ||
                keys[1] !== 'product_id') return null;

            var importedProductId = target.imported_product_id;
            var wbProductId = target.product_id;
            var updatedIndex = updatedIndexById.get(importedProductId);
            if (!Number.isSafeInteger(importedProductId) || importedProductId <= 0 ||
                !Number.isSafeInteger(wbProductId) || wbProductId <= 0 ||
                updatedIndex === undefined || updatedIndex <= lastUpdatedIndex ||
                seenWbProductIds.has(wbProductId)) {
                return null;
            }

            lastUpdatedIndex = updatedIndex;
            seenWbProductIds.add(wbProductId);
            productIds.push(wbProductId);
        }
        return productIds;
    }

    function supplierRefreshWbExtension(data, updatedIds) {
        var productIds = exactSupplierRefreshWbProductIds(data, updatedIds);
        if (!productIds) return { productIds: null, unavailable: 0 };
        if (data.wb_unavailable === undefined) {
            return { productIds: productIds, unavailable: 0 };
        }
        if (!nonnegativeCount(data.wb_unavailable) ||
            !nonnegativeCount(data.wb_ready) ||
            data.wb_ready + data.wb_unavailable > updatedIds.length) {
            return { productIds: null, unavailable: 0 };
        }
        return { productIds: productIds, unavailable: data.wb_unavailable };
    }

    function supplierRefreshOutcome(data, ids, labels, commonContentUrl) {
        if (!data || data.success !== true ||
            !['completed', 'completed_with_errors'].includes(data.status) ||
            !nonnegativeCount(data.requested) || data.requested !== ids.length ||
            !nonnegativeCount(data.updated) || !nonnegativeCount(data.skipped) ||
            !nonnegativeCount(data.errors) ||
            data.updated + data.skipped + data.errors !== data.requested ||
            (data.status === 'completed') !== (data.errors === 0) ||
            !Array.isArray(data.items) || data.items.length !== ids.length) return null;

        var submitted = new Set(ids);
        var rowsById = new Map();
        var itemCounts = { updated: 0, skipped: 0, error: 0 };
        for (var i = 0; i < data.items.length; i += 1) {
            var item = data.items[i];
            if (!item || !Number.isSafeInteger(item.product_id) ||
                !submitted.has(item.product_id) || rowsById.has(item.product_id)) return null;

            var row;
            if (item.status === 'updated' && item.reason === null) {
                row = {
                    status: 'updated',
                    statusLabel: 'Обновлено',
                    reasonLabel: 'Данные поставщика сохранены в Seller Hub.',
                    tone: 'ok',
                };
            } else if (item.status === 'skipped' && item.reason === 'no_supplier_link') {
                row = {
                    status: 'skipped',
                    statusLabel: 'Пропущено',
                    reasonLabel: 'Нет связи с каталогом поставщика. Данные не менялись.',
                    tone: 'muted',
                };
            } else if (item.status === 'error' && item.reason === 'update_failed') {
                row = {
                    status: 'error',
                    statusLabel: 'Не обновлено',
                    reasonLabel: 'Не удалось сохранить данные. Откройте карточку и проверьте её.',
                    tone: 'danger',
                    href: commonContentUrl
                        ? commonContentUrl + '?product_id=' + encodeURIComponent(item.product_id)
                        : '',
                };
            } else {
                return null;
            }
            itemCounts[row.status] += 1;
            rowsById.set(item.product_id, Object.assign({
                productId: item.product_id,
                label: labels.find(function (entry) { return entry.productId === item.product_id; })?.label
                    || 'Товар из выбранного списка',
            }, row));
        }

        if (rowsById.size !== ids.length || itemCounts.updated !== data.updated ||
            itemCounts.skipped !== data.skipped || itemCounts.error !== data.errors) return null;

        var summary = 'В запросе товаров: ' + data.requested + '.';

        var title = data.errors
            ? 'Обновление завершено с ошибками'
            : data.updated === 0 && data.skipped > 0
                ? 'Нет карточек для обновления'
                : 'Обновление от поставщика завершено';
        var updatedIds = ids.filter(function (id) {
            return rowsById.get(id).status === 'updated';
        });
        var wbExtension = supplierRefreshWbExtension(data, updatedIds);
        if (wbExtension.unavailable > 0) {
            summary += ' Недоступны исходные ссылки WB: ' + wbExtension.unavailable + '.';
        }
        var updatedBatchValid = validSupplierUpdatedBatchToken(data.updated_batch);
        var outcome = {
            kind: data.errors ? 'partial' : 'complete',
            title: title,
            summary: summary,
            requested: data.requested,
            updated: data.updated,
            skipped: data.skipped,
            errors: data.errors,
            wbUnavailable: wbExtension.unavailable,
            entries: ids.map(function (id) { return rowsById.get(id); }),
            wbProductIds: wbExtension.productIds,
            updatedBatch: updatedBatchValid ? data.updated_batch : null,
        };
        if (data.updated > 0 && !updatedBatchValid) {
            outcome.resumeWarning = 'Список обновлённых товаров не удалось подтвердить. Массовый выбор недоступен.';
        }
        return outcome;
    }

    function exactSupplierRefreshResumeLabels(data) {
        if (!data || !Array.isArray(data.labels) ||
            data.labels.length < 1 || data.labels.length > MAX_BULK_SELECTION) return null;
        var ids = [];
        var labels = [];
        var seen = new Set();
        for (var i = 0; i < data.labels.length; i += 1) {
            var entry = data.labels[i];
            if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return null;
            var keys = Object.keys(entry).sort();
            if (keys.length !== 2 || keys[0] !== 'label' || keys[1] !== 'productId' ||
                !Number.isSafeInteger(entry.productId) || entry.productId <= 0 ||
                seen.has(entry.productId) || typeof entry.label !== 'string' ||
                !entry.label.trim() || entry.label.length > 2048) return null;
            seen.add(entry.productId);
            ids.push(entry.productId);
            labels.push({ productId: entry.productId, label: entry.label });
        }
        return { ids: ids, labels: labels };
    }

    function restoredSupplierRefreshOutcome(data, commonContentUrl) {
        var submitted = exactSupplierRefreshResumeLabels(data);
        if (!submitted) return null;
        var outcome = supplierRefreshOutcome(data, submitted.ids, submitted.labels, commonContentUrl);
        if (!outcome) return null;
        outcome.saved = true;
        outcome.title = 'Сохранённый итог обновления от поставщика';
        return outcome;
    }

    function supplierRefreshNotice(kind, requested, message) {
        var rejected = kind === 'rejected';
        return {
            kind: kind,
            title: rejected ? 'Выбранные товары не обновлялись' : 'Итог обновления не подтверждён',
            summary: 'В запросе товаров: ' + requested + '. ' + message,
            requested: requested,
            entries: [],
        };
    }

    var SUPPLIER_REFRESH_PREVIEW_FIELDS = {
        title: 'Название',
        purchase_price: 'Закупочная цена',
        supplier_quantity: 'Остаток у поставщика',
        description: 'Описание',
        photos: 'Фото',
        characteristics: 'Характеристики',
        category: 'Категория',
        product_details: 'Подробности товара',
        source_snapshot: 'Данные источника',
    };
    var SUPPLIER_REFRESH_GROUP_FIELDS = new Set([
        'description', 'photos', 'characteristics', 'category',
        'product_details', 'source_snapshot',
    ]);
    function validSupplierRefreshPreview(data, ids) {
        if (!data || data.success !== true || !Array.isArray(ids) || !ids.length ||
            !nonnegativeCount(data.requested) || data.requested !== ids.length ||
            !nonnegativeCount(data.eligible) || !nonnegativeCount(data.changed) ||
            !nonnegativeCount(data.skipped) ||
            data.eligible + data.skipped !== data.requested ||
            data.changed > data.eligible || !Array.isArray(data.items) ||
            data.items.length !== ids.length || typeof data.review_token !== 'string' ||
            !data.review_token.trim() || data.review_token.length > 8192 ||
            /[\u0000-\u001f\u007f]/.test(data.review_token)) return null;

        var changedCount = 0;
        var skippedCount = 0;
        var eligibleCount = 0;
        var items = [];
        for (var i = 0; i < data.items.length; i += 1) {
            var item = data.items[i];
            if (!item || !Number.isSafeInteger(item.product_id) ||
                item.product_id !== ids[i] || typeof item.label !== 'string' ||
                !item.label.trim() || item.label.length > 2048 ||
                !['changed', 'unchanged', 'unlinked'].includes(item.state) ||
                !Array.isArray(item.changes) || item.changes.length > 9) return null;

            var seenFields = new Set();
            var changes = [];
            for (var j = 0; j < item.changes.length; j += 1) {
                var change = item.changes[j];
                if (!change || !Object.prototype.hasOwnProperty.call(
                        SUPPLIER_REFRESH_PREVIEW_FIELDS, change.field) ||
                    seenFields.has(change.field)) return null;
                var groupOnly = SUPPLIER_REFRESH_GROUP_FIELDS.has(change.field);
                if (groupOnly) {
                    if (change.before !== null || change.after !== null) return null;
                } else if (![null, 'string', 'number'].includes(
                        change.before === null ? null : typeof change.before) ||
                    ![null, 'string', 'number'].includes(
                        change.after === null ? null : typeof change.after) ||
                    (typeof change.before === 'number' && !Number.isFinite(change.before)) ||
                    (typeof change.after === 'number' && !Number.isFinite(change.after)) ||
                    (typeof change.before === 'string' && change.before.length > 2048) ||
                    (typeof change.after === 'string' && change.after.length > 2048)) return null;
                seenFields.add(change.field);
                changes.push({
                    field: change.field,
                    label: SUPPLIER_REFRESH_PREVIEW_FIELDS[change.field],
                    groupOnly: groupOnly,
                    before: change.before,
                    after: change.after,
                });
            }

            if ((item.state === 'changed' && !changes.length) ||
                (item.state !== 'changed' && changes.length)) return null;
            if (item.state === 'changed') changedCount += 1;
            else if (item.state === 'unlinked') skippedCount += 1;
            else eligibleCount += 1;
            if (item.state === 'changed') eligibleCount += 1;
            items.push({
                productId: item.product_id,
                label: item.label,
                state: item.state,
                stateLabel: item.state === 'changed'
                    ? 'Есть изменения'
                    : item.state === 'unchanged'
                        ? 'Без изменений'
                        : 'Нет связи с каталогом поставщика',
                changes: changes,
            });
        }
        if (changedCount !== data.changed || skippedCount !== data.skipped ||
            eligibleCount !== data.eligible) return null;

        return {
            requested: data.requested,
            eligible: data.eligible,
            changed: data.changed,
            skipped: data.skipped,
            items: items,
            reviewToken: data.review_token,
        };
    }

    function supplierRefreshPreviewValue(value) {
        if (value === null || value === '') return '—';
        var text = String(value).replace(/\s+/g, ' ').trim();
        if (!text) return '—';
        return text.length > 160 ? text.slice(0, 157) + '…' : text;
    }

    var WB_STATUS_FILTERS = [
        { key: 'pending', label: 'Ожидают проверки', countKey: 'pending' },
        { key: 'validated', label: 'Готовы к отправке в WB', countKey: 'validated' },
        { key: 'imported', label: 'Переданы в WB', countKey: 'imported' },
        { key: 'imported_pending_sync', label: 'Ждут артикул WB', countKey: 'imported_pending_sync' },
        { key: 'failed', label: 'Не удалось подготовить', countKey: 'failed' },
    ];

    function validWorkspaceReturnScope(scope) {
        if (!scope || typeof scope !== 'object' || Array.isArray(scope)) return false;
        var keys = Object.keys(scope).sort();
        var expectedKeys = WORKSPACE_RETURN_SCOPE_KEYS.slice().sort();
        var allowedKeys = WORKSPACE_RETURN_SCOPE_KEYS.concat(WORKSPACE_RETURN_SCOPE_OPTIONAL_KEYS);
        if (keys.length < expectedKeys.length || keys.length > allowedKeys.length ||
            !expectedKeys.every(function (key) { return keys.indexOf(key) !== -1; }) ||
            !keys.every(function (key) { return allowedKeys.indexOf(key) !== -1; })) return false;
        var supplierIsActive = scope.supplier === '' || (
            Number.isSafeInteger(scope.supplier) && scope.supplier > 0 &&
            Array.isArray(boot.suppliers) && boot.suppliers.some(function (supplier) {
                return supplier && supplier.id === scope.supplier;
            })
        );
        var marketplaceValid = !Object.prototype.hasOwnProperty.call(scope, 'marketplace') ||
            scope.marketplace === 'wb' ||
            (scope.marketplace === 'ozon' && boot.ozonEnabled === true);
        var marketplaceStateValid = !Object.prototype.hasOwnProperty.call(scope, 'marketplace_state') ||
            ['active', 'preparation', 'attention'].indexOf(scope.marketplace_state) !== -1;
        var catalogCategoryValid = !Object.prototype.hasOwnProperty.call(scope, 'catalog_category') ||
            validCatalogCategoryToken(scope.catalog_category);
        var activeTabValid = scope.activeTab === '' || scope.activeTab === 'updates' ||
            WB_STATUS_FILTERS.some(function (filter) { return filter.key === scope.activeTab; });
        var marketplaceStatusCompatible = scope.marketplace !== 'ozon' ||
            scope.activeTab === '' || scope.activeTab === 'updates';
        return activeTabValid && marketplaceStatusCompatible && catalogCategoryValid &&
            typeof scope.search === 'string' && scope.search.length <= 200 &&
            scope.search.trim() === scope.search && supplierIsActive &&
            marketplaceValid && marketplaceStateValid &&
            ['', 'yes', 'no'].indexOf(scope.has_photos) !== -1 &&
            ['', 'in_stock', 'out_of_stock'].indexOf(scope.stock) !== -1 &&
            ['', 'oldest', 'price_asc', 'price_desc', 'title'].indexOf(scope.sort) !== -1 &&
            Number.isSafeInteger(scope.visibleCount) &&
            scope.visibleCount >= 1 && scope.visibleCount <= 200;
    }

    function defaultWorkspaceReturnScope(scope) {
        return scope.activeTab === '' && scope.search === '' && scope.supplier === '' &&
            scope.has_photos === '' && scope.stock === '' && scope.sort === '' &&
            !Object.prototype.hasOwnProperty.call(scope, 'marketplace') &&
            !Object.prototype.hasOwnProperty.call(scope, 'marketplace_state') &&
            !Object.prototype.hasOwnProperty.call(scope, 'catalog_category');
    }

    function workspaceReturnScopeSnapshot(app, visibleCount) {
        var filters = app.filters || {};
        var scope = {
            activeTab: app.activeTab,
            search: filters.search,
            supplier: filters.supplier,
            has_photos: filters.has_photos,
            stock: filters.stock,
            sort: filters.sort,
            visibleCount: visibleCount,
        };
        if (filters.marketplace) scope.marketplace = filters.marketplace;
        if (filters.marketplace_state) scope.marketplace_state = filters.marketplace_state;
        if (filters.catalog_category) scope.catalog_category = filters.catalog_category;
        return scope;
    }

    function workspaceReturnScopeMatchesCurrent(app, scope) {
        var filters = app && app.filters || {};
        if (!app || !scope || app.loading || app.loadingMore || app.selectAllLoading ||
            typeof app.feedScopeKey !== 'function' || app.itemsScope !== app.feedScopeKey() ||
            (app.searchInput || '').trim().slice(0, 200) !== filters.search ||
            !Array.isArray(app.items) ||
            !validWorkspaceReturnScope(scope)) return false;
        var visibleCount = Math.min(app.items.length, MAX_BULK_SELECTION);
        if (!visibleCount || !orderedProductIds(app.items.slice(0, visibleCount))) return false;
        var current = workspaceReturnScopeSnapshot(app, visibleCount);
        var currentKeys = Object.keys(current).sort();
        var expectedKeys = Object.keys(scope).sort();
        var sameScope = currentKeys.length === expectedKeys.length &&
            currentKeys.every(function (key, index) {
                return key === expectedKeys[index] && current[key] === scope[key];
            });
        return sameScope && (!app.importBatchMode || defaultWorkspaceReturnScope(scope));
    }

    function commonContentSelectionUrl(base, productIds, helpers) {
        if (helpers && typeof helpers.selectionUrl === 'function') {
            return helpers.selectionUrl(base, productIds);
        }
        if (typeof base !== 'string' || base.charAt(0) !== '/' || base.indexOf('//') === 0 ||
            base.indexOf('?') !== -1 || base.indexOf('#') !== -1 ||
            !Array.isArray(productIds) || !productIds.length || productIds.length > 50) {
            throw new TypeError('A valid same-site selection URL is required.');
        }
        var ids = new Set();
        var query = new URLSearchParams();
        productIds.forEach(function (id) {
            if (!Number.isSafeInteger(id) || id <= 0 || ids.has(id)) {
                throw new TypeError('Product IDs must be unique positive integers.');
            }
            ids.add(id);
            query.append('product_id', String(id));
        });
        return base + '?' + query.toString();
    }

    var workspaceReturnCapsule = null;
    var workspaceReturnScopeSeed = null;
    var workspaceReturnSetupNote = '';
    var workspaceChangedNote = 'Список изменился. Проверьте его и отметьте товары заново.';
    var workspaceHelpers = window.SellerHubCommonContent || {};
    if (window.location && typeof window.location.hash === 'string' &&
        typeof workspaceHelpers.workspaceReturnFromHash === 'function') {
        try {
            workspaceReturnCapsule = workspaceHelpers.workspaceReturnFromHash(
                window.location.hash, { owner: boot.workspaceOwner }
            );
        } catch (_) {
            workspaceReturnCapsule = null;
        }
    }
    if (workspaceReturnCapsule && !supplierRefreshBatchMode) {
        var expectedWorkspaceBatch = importBatchMode && !importBatchInvalid
            ? importBatchToken : null;
        var batchMatches = importBatchMode
            ? !importBatchInvalid && workspaceReturnCapsule.batch === expectedWorkspaceBatch
            : workspaceReturnCapsule.batch === null;
        var scopeIsValid = validWorkspaceReturnScope(workspaceReturnCapsule.scope);
        var batchScopeIsValid = !workspaceReturnCapsule.batch || (
            scopeIsValid && defaultWorkspaceReturnScope(workspaceReturnCapsule.scope)
        );
        if (batchMatches && scopeIsValid && batchScopeIsValid) {
            workspaceReturnScopeSeed = workspaceReturnCapsule.scope;
        } else {
            workspaceReturnSetupNote = workspaceChangedNote;
        }
    }
    var workspaceSelectionReviewReturn = readSelectionReviewReturn(workspaceReturnCapsule);

    window.shVue.mount('#my-products-app', {
        data: function () {
            var savedOzon = readOzonPending();
            var savedInlineOzon = readOzonInlineReceipt();
            var savedWbCreate = readWbCreatePending();
            return {
                wbConnected: !!boot.wbConnected,
                wbAccountId: wbAccountIdFromBootstrap,
                wbAccountKnown: wbAccountIdFromBootstrap !== undefined,
                wbEditBusy: false,
                ozonEnabled: !!boot.ozonEnabled,
                ozonAccounts: boot.ozonAccounts || [],
                suppliers: boot.suppliers || [],
                importSuppliers: boot.importSuppliers || [],
                workspaceOwner: typeof boot.workspaceOwner === 'string' ? boot.workspaceOwner : '',
                urls: boot.urls || {},
                importUrls: {
                    workspace: (boot.urls || {}).workspace || '',
                    supplierCatalog: (boot.urls || {}).supplierCatalog || '',
                    importPost: (boot.urls || {}).supplierImportPost || '',
                    importResult: (boot.urls || {}).supplierImportResult || '',
                },
                importPanelAvailable: importPanelAvailable,
                priceWorkspaceDialogAvailable: priceWorkspaceDialogAvailable,
                priceWorkspaceDialogUrls: {
                    wbPreview: priceWorkspaceUrls.wbPreview || '',
                    wbSave: priceWorkspaceUrls.wbSave || '',
                    ozonPreview: priceWorkspaceUrls.ozonPreview || '',
                    ozonSave: priceWorkspaceUrls.ozonSave || '',
                },
                priceWorkspaceDialogOpen: false,
                priceWorkspaceScope: null,
                priceWorkspaceReviewHref: '',
                priceWorkspaceLaunch: null,
                priceWorkspaceReturnFocus: null,
                supplierImportOpen: false,
                supplierImportOutcome: null,
                supplierImportFallbackUrl: '',
                importBatchSelectionPending: false,
                importBatchExpectedCount: 0,
                importBatchSelectionVerified: false,
                inlineSupplierImportToken: inlineImportMarkerToken(),
                _importSelectionBeforeOpen: null,
                importBatch: importBatchToken,
                importBatchMode: importBatchMode,
                importBatchKind: importBatchKind,
                importBatchInvalid: importBatchInvalid,
                supplierRefreshResultBatchMode: supplierRefreshResultBatchMode,
                supplierRefreshSelectPending: false,
                supplierRefreshBatch: supplierRefreshBatchToken,
                supplierRefreshBatchMode: supplierRefreshBatchMode,
                supplierRefreshBatchInvalid: supplierRefreshBatchInvalid,
                supplierRefreshReturnUrl: typeof boot.supplierRefreshReturnUrl === 'string'
                    ? boot.supplierRefreshReturnUrl : (boot.urls || {}).classic || '/my-products',

                catalogCategoryOptions: [],
                catalogCategoriesPage: 0,
                catalogCategoriesHasNext: false,
                catalogCategoriesHasUncategorized: false,
                catalogCategoriesLoading: false,
                catalogCategoriesError: '',
                _catalogCategoriesRequestId: 0,
                _catalogCategoriesAbort: null,

                items: [],
                itemsScope: '',
                facets: {},
                pagination: {},
                loading: true,
                loadingMore: false,
                selectAllLoading: false,
                error: '',
                workspaceReturnNote: workspaceReturnSetupNote,
                workspaceReturnPending: !!workspaceReturnScopeSeed,

                activeTab: workspaceReturnScopeSeed ? workspaceReturnScopeSeed.activeTab : '',
                searchInput: workspaceReturnScopeSeed ? workspaceReturnScopeSeed.search : '',
                filters: {
                    search: workspaceReturnScopeSeed ? workspaceReturnScopeSeed.search : '',
                    supplier: workspaceReturnScopeSeed ? workspaceReturnScopeSeed.supplier : '',
                    has_photos: workspaceReturnScopeSeed ? workspaceReturnScopeSeed.has_photos : '',
                    stock: workspaceReturnScopeSeed ? workspaceReturnScopeSeed.stock : '',
                    sort: workspaceReturnScopeSeed ? workspaceReturnScopeSeed.sort : '',
                    marketplace: workspaceReturnScopeSeed && workspaceReturnScopeSeed.marketplace
                        ? workspaceReturnScopeSeed.marketplace : '',
                    marketplace_state: workspaceReturnScopeSeed && workspaceReturnScopeSeed.marketplace_state
                        ? workspaceReturnScopeSeed.marketplace_state : '',
                    catalog_category: workspaceReturnScopeSeed && workspaceReturnScopeSeed.catalog_category
                        ? workspaceReturnScopeSeed.catalog_category : '',
                },
                selected: [],
                selectionReviewActive: false,
                selectionReviewLoading: false,
                selectionReviewRows: [],
                selectionReviewIds: [],
                selectionReviewError: '',
                selectionReviewUnavailableIds: [],
                activeBulkActionGroup: '',
                ozonIds: [],
                ozonAccountId: '',
                ozonInlineContextAtOpen: null,
                ozonSelectionAtOpen: null,
                ozonSelectionScopeAtOpen: '',
                ozonBusy: '',
                ozonError: '',
                ozonReturnFocus: null,
                ozonPending: validOzonPending(savedOzon, boot.ozonAccounts || []) ? savedOzon : null,
                ozonStorageProblem: hasOzonPending() && !validOzonPending(savedOzon, boot.ozonAccounts || []),
                ozonInlineReceipt: savedInlineOzon.receipt,
                ozonInlineReceiptStorageProblem: savedInlineOzon.storageError,
                ozonInlineReceiptFocusPending: false,
                ozonRetrySameKey: false,
                ozonSessionEnded: false,
                wbCreatePending: savedWbCreate.pending,
                wbCreateTerminalResult: null,
                wbCreateReceiptFocusSet: false,
                wbCreateStorageProblem: savedWbCreate.storageProblem,
                wbCreateBusy: false,
                wbCreateReading: false,
                wbCreateRetrySameKey: false,
                wbCreateError: '',
                wbCreateReadError: '',
                wbCreateTerminalReceiptVerified: false,
                wbCreateSelectionIntentRevision: 0,
                _wbCreateReadAfterPost: false,
                _wbCreatePostAbort: null,
                _wbCreatePostTimer: null,
                _wbCreateReadAbort: null,
                _wbCreateReadTimer: null,
                _wbCreatePollTimer: null,
                _mpUnmounted: false,
                ozonPriceError: '',
                ozonPriceAccountId: '',
                ozonPriceHandoffReview: null,
                ozonPriceDialogError: '',
                ozonAccountChooserKind: '',
                ozonWorkspaceUpdateReview: null,
                ozonWorkspaceUpdateBusy: false,
                ozonWorkspaceUpdateDialogError: '',
                imgFailed: {},
                wbStatusFilters: WB_STATUS_FILTERS,

                job: null,
                jobTitle: 'Выполняем',
                supplierRefreshResult: null,
                supplierRefreshReview: null,
                supplierRefreshResumeNote: '',
                supplierRefreshResumeRetry: false,
                supplierRefreshRestoreLoading: false,
                confirm: {
                    open: false, key: null, title: '', message: '',
                    details: [], label: 'Продолжить', danger: false, busy: false,
                    productIds: [], selectionIds: [], labels: [],
                },
            };
        },

        computed: {
            catalogCategoryMoreOption: function () {
                return CATALOG_CATEGORY_MORE_OPTION;
            },
            catalogCategorySelectOptions: function () {
                var options = this.catalogCategoryOptions.slice();
                if ((this.catalogCategoriesHasUncategorized || this.filters.catalog_category === 'missing') &&
                    !options.some(function (option) { return option.key === 'missing'; })) {
                    options.unshift({ key: 'missing', label: 'Без категории' });
                }
                var selected = this.filters.catalog_category;
                if (selected && validCatalogCategoryToken(selected) &&
                    !options.some(function (option) { return option.key === selected; })) {
                    options.unshift({
                        key: selected,
                        label: selected === 'missing' ? 'Без категории' : selected.slice(6),
                    });
                }
                return options;
            },
            selectedWbStatusLabel: function () {
                var selected = this.activeTab === 'updates' ? '' : this.activeTab;
                var option = WB_STATUS_FILTERS.find(function (filter) { return filter.key === selected; });
                return option ? option.label : '';
            },
            activeAdvancedFilterCount: function () {
                return (this.filters.has_photos ? 1 : 0) +
                    (this.filters.stock ? 1 : 0) + (this.filters.supplier ? 1 : 0) +
                    (this.selectedWbStatusLabel ? 1 : 0);
            },
            extraFiltersDisclosureLabel: function () {
                var labels = [];
                var selectedSupplier = (this.suppliers || []).find(function (supplier) {
                    return supplier && supplier.id === this.filters.supplier;
                }, this);
                if (this.filters.supplier) {
                    labels.push('Поставщик: ' + (selectedSupplier ? selectedSupplier.name : 'выбран'));
                }
                if (this.filters.has_photos === 'yes') labels.push('С фото');
                else if (this.filters.has_photos === 'no') labels.push('Без фото');
                if (this.filters.stock === 'in_stock') labels.push('В наличии у поставщика');
                else if (this.filters.stock === 'out_of_stock') labels.push('Нет у поставщика');
                if (this.selectedWbStatusLabel) labels.push('Статус WB: ' + this.selectedWbStatusLabel);
                return labels.length
                    ? 'Дополнительные фильтры. Выбрано: ' + labels.join(', ')
                    : 'Дополнительные фильтры';
            },
            supplierUpdatesEntryAriaLabel: function () {
                if (this.selected.length) {
                    var selectedCount = this.selected.length;
                    return 'Обновить выбранные товары, ' + selectedCount + ' ' + this.$fmt.plural(
                        selectedCount, 'товар', 'товара', 'товаров',
                    ) + ' из каталога поставщика';
                }
                var count = this.facets.supplier_updates;
                return Number.isSafeInteger(count) && count >= 0
                    ? 'Обновления поставщика, ' + count + ' ' + this.$fmt.plural(count, 'товар', 'товара', 'товаров')
                    : 'Обновления поставщика';
            },
            supplierRefreshSelectionAction: function () {
                var group = (this.bulkIntentGroupsForDisplay || []).find(function (entry) {
                    return entry.key === 'refresh';
                });
                return group ? group.actions.find(function (action) {
                    return action.key === 'refresh_supplier';
                }) || null : null;
            },
            supplierUpdatesEntryDisabled: function () {
                if (this.workspaceReturnPending || this.loading || this.loadingMore || this.selectAllLoading ||
                    this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) return true;
                if (!this.selected.length) return false;
                var action = this.supplierRefreshSelectionAction;
                return !action || action.disabled || this.selectedItems.length !== this.selected.length;
            },
            allSelected: function () {
                var chosen = this.selected;
                return this.items.length > 0 && this.items.every(function (item) {
                    return chosen.indexOf(item.id) !== -1;
                });
            },
            someSelected: function () {
                var chosen = this.selected;
                var visibleSelected = this.items.filter(function (item) {
                    return chosen.indexOf(item.id) !== -1;
                }).length;
                return visibleSelected > 0 && visibleSelected < this.items.length;
            },
            selectedItems: function () {
                return this.selectionRowsVerified ? this.selectionItemsForIds(this.selected) : [];
            },
            visibleItems: function () {
                if (!this.selectionReviewActive) return this.items;
                var chosen = this.selected;
                return this.selectionReviewRows.filter(function (item) {
                    return chosen.indexOf(item.id) !== -1;
                });
            },
            selectionRowsVerified: function () {
                var ids = this.selected;
                if (!validSelectionReviewIds(ids) || this.selectionReviewLoading || this.loading ||
                    this.loadingMore || this.selectAllLoading || this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search ||
                    (validSelectionReviewIds(this.selectionReviewUnavailableIds) &&
                        sameIds(ids, this.selectionReviewUnavailableIds))) return false;
                if (this.importBatchMode && this.importBatchKind === 'import' &&
                    !this.importBatchSelectionVerified) return false;
                if (this.selectionReviewActive && !ids.every((id) => this.selectionReviewIds.indexOf(id) !== -1)) return false;
                var available = new Set();
                (this.items || []).forEach(function (item) {
                    if (item && Number.isSafeInteger(item.id) && item.id > 0) available.add(item.id);
                });
                if (validSelectionReviewIds(this.selectionReviewIds)) {
                    this.selectionReviewIds.forEach(function (id) { available.add(id); });
                }
                return ids.every(function (id) { return available.has(id); });
            },
            selectedOutsideCurrentListCount: function () {
                var visible = new Set((this.items || []).map(function (item) { return item && item.id; }));
                return this.selected.filter(function (id) { return !visible.has(id); }).length;
            },
            selectionReviewAvailable: function () {
                return !this.importBatchMode && !this.supplierRefreshBatchMode &&
                    this.selected.length > 0 && this.selected.length <= MAX_BULK_SELECTION &&
                    typeof this.urls.selection === 'string' && this.urls.selection.length > 0 &&
                    !this.loading && !this.loadingMore && !this.selectAllLoading &&
                    !this.selectionReviewLoading && this.itemsScope === this.feedScopeKey() &&
                    (this.searchInput || '').trim().slice(0, 200) === this.filters.search;
            },
            hasUnshownResults: function () {
                return this.itemsScope === this.feedScopeKey() &&
                    Number.isSafeInteger(this.pagination.total) &&
                    this.pagination.total > this.items.length;
            },
            canSelectAllFiltered: function () {
                return !this.importBatchMode && !this.supplierRefreshBatchMode &&
                    !this.loading && !this.loadingMore &&
                    this.itemsScope === this.feedScopeKey() &&
                    (this.searchInput || '').trim().slice(0, 200) === this.filters.search &&
                    Number.isSafeInteger(this.pagination.total) &&
                    this.pagination.total > this.items.length &&
                    this.pagination.total > 0 &&
                    this.pagination.total <= MAX_BULK_SELECTION;
            },
            supplierRefreshOzonIds: function () {
                var result = this.supplierRefreshResult;
                if (!result || !['complete', 'partial'].includes(result.kind) ||
                    !Array.isArray(result.entries)) return [];

                var updatedIds = result.entries
                    .filter(function (entry) { return entry && entry.status === 'updated'; })
                    .map(function (entry) { return entry.productId; });
                return updatedIds.length === result.updated ? updatedIds : [];
            },
            supplierRefreshCanSelectUpdated: function () {
                var result = this.supplierRefreshResult;
                return !!result && ['complete', 'partial'].includes(result.kind) &&
                    Number.isSafeInteger(result.updated) && result.updated > 0 &&
                    validSupplierUpdatedBatchToken(result.updatedBatch);
            },
            supplierRefreshWbProductIds: function () {
                var result = this.supplierRefreshResult;
                var productIds = result && result.wbProductIds;
                if (!this.wbConnected || !result ||
                    !['complete', 'partial'].includes(result.kind) ||
                    !Array.isArray(productIds) || !productIds.length) return [];
                return productIds;
            },
            ozonPreparationDisabled: function () {
                return !this.ozonAccounts.length || !!this.ozonPending ||
                    this.ozonStorageProblem || !!this.ozonBusy || this.ozonSessionEnded;
            },
            /* Shared route availability stays tied to the existing guarded actions. */
            actionGroups: function () {
                var chosen = this.selectedItems;
                var createRows = chosen.filter(function (item) {
                    return !item.wb && ['pending', 'failed', 'validated'].indexOf(item.import_status) !== -1;
                });
                var wbCreateLocked = this.wbCreateStorageProblem || !!this.wbCreatePending;
                var wbIds = exactWbProductIds(chosen);
                var withUpdates = chosen.filter(function (i) { return i.has_supplier_update; }).length;
                var underLimit = chosen.length <= MAX_BULK_SELECTION;
                var workspaceCurrent = this.selectionRowsVerified && !this.loading && !this.loadingMore && !this.selectAllLoading &&
                    this.itemsScope === this.feedScopeKey() &&
                    (this.searchInput || '').trim().slice(0, 200) === this.filters.search;
                var ozonUpdateResult = exactOzonWorkspaceUpdateAccountChoices(
                    chosen, this.selected, this.ozonAccounts,
                );
                var ozonPriceResult = exactOzonPriceAccountChoices(
                    chosen, this.selected, this.ozonAccounts,
                );
                var ozonPriceHint = ozonPriceResult.reason === 'no_accounts'
                    ? 'подключите активный магазин Ozon'
                    : ozonPriceResult.reason === 'accounts'
                        ? 'не удалось проверить магазины Ozon; обновите страницу'
                        : ozonPriceResult.reason === 'selection'
                            ? 'список изменился или загружен не полностью; обновите его'
                            : ozonPriceResult.reason === 'limit'
                                ? 'за одно сравнение можно выбрать не больше 100 товаров'
                                : 'не во всех товарах есть связанная карточка в одном магазине Ozon';
                var ozonUpdateHint = !chosen.length
                    ? 'выберите товары с уже связанной карточкой Ozon'
                    : !underLimit
                        ? 'за один раз можно выбрать не больше 200 товаров'
                        : !workspaceCurrent
                            ? 'дождитесь загрузки списка и повторите действие'
                            : ozonUpdateResult.reason === 'no_accounts'
                                ? 'подключите активный магазин Ozon'
                                : ozonUpdateResult.reason === 'accounts'
                                    ? 'не удалось проверить магазины Ozon; обновите страницу'
                                    : ozonUpdateResult.reason === 'selection'
                                        ? 'список изменился или загружен не полностью; обновите его'
                                        : !ozonUpdateResult.choices.length
                                            ? 'не у всех товаров есть точная доступная карточка в одном магазине Ozon'
                                            : this.ozonWorkspaceUpdateBusy
                                                ? 'проверяем точный состав выбранных карточек…'
                                                : ozonUpdateResult.choices.length > 1
                                                    ? 'выберите магазин, где связаны все карточки'
                                                    : 'сначала проверим выбранные карточки; применение подтвердите отдельно';
                var ozonUpdateDisabled = !chosen.length || !underLimit || !workspaceCurrent ||
                    !ozonUpdateResult.choices.length || this.ozonWorkspaceUpdateBusy;
                var selectionReady = this.selectionRowsVerified;
                var selectionHint = this.selected.length > MAX_BULK_SELECTION
                    ? 'За один запуск можно выбрать не больше 200 товаров.'
                    : this.selectionReviewLoading
                        ? 'проверяем точный список выбранных товаров…'
                        : 'Сначала проверьте выбранный список кнопкой «Показать выбранные». Товары вне списка не будут включены автоматически.';
                return [{
                    title: 'Основные действия',
                    items: [
                        {
                            key: 'push_wb',
                            label: 'Создать карточки WB',
                            hint: wbCreateLocked
                                ? 'сначала проверьте сохранённый результат предыдущего запуска WB'
                                : !this.wbConnected
                                ? 'подключите кабинет Wildberries'
                                : !underLimit
                                ? 'за один запуск можно выбрать не больше 200 товаров'
                                : createRows.length
                                    ? 'для ' + createRows.length + ' товаров, ещё не связанных с WB'
                                    : 'выберите товары, которые ещё не связаны с WB',
                            disabled: wbCreateLocked || !this.wbConnected || !underLimit || !createRows.length,
                        },
                        this.ozonEnabled ? {
                            key: 'ozon',
                            label: 'Подготовить для Ozon',
                            hint: !underLimit
                                ? 'за один запуск можно выбрать не больше 200 товаров'
                                : !this.ozonAccounts.length
                                    ? 'сначала подключите магазин Ozon'
                                    : this.ozonBusy
                                        ? 'дождитесь завершения текущего действия'
                                    : this.ozonPending || this.ozonStorageProblem || this.ozonSessionEnded
                                        ? 'сначала проверьте сохранённый результат подготовки'
                                : 'локальные черновики в выбранном магазине; отправка подтверждается отдельно',
                            disabled: !underLimit || this.ozonPreparationDisabled,
                        } : null,
                        {
                            key: 'refresh_supplier',
                            label: 'Обновить из поставщика',
                            hint: !underLimit
                                ? 'за один запуск можно выбрать не больше 200 товаров'
                                : withUpdates
                                    ? withUpdates + ' с новыми данными; изменения останутся в Seller Hub'
                                    : 'обновит данные только в Seller Hub; карточки WB и Ozon не меняются',
                            disabled: !underLimit,
                        },
                        {
                            key: 'common_content',
                            label: 'Изменить данные товара',
                            hint: chosen.length > 50
                                ? 'за один раз можно открыть не больше 50 товаров'
                                : 'изменит данные товара в Seller Hub; карточки WB и Ozon не обновятся',
                            disabled: !chosen.length || chosen.length > 50,
                        },
                        {
                            key: 'wb_edit',
                            label: 'Изменить карточки WB',
                            hint: !chosen.length
                                ? 'выберите опубликованные карточки WB'
                                : !underLimit
                                    ? 'за один запуск можно выбрать не больше 200 товаров'
                                    : !this.wbConnected
                                        ? 'подключите кабинет Wildberries'
                                        : !this.wbAccountKnown
                                            ? 'не удалось проверить аккаунт WB; обновите страницу'
                                            : !workspaceCurrent
                                                ? 'дождитесь загрузки списка, затем повторите действие'
                                            : chosen.some(function (item) { return !item.wb; })
                                                ? 'снимите товары без опубликованной карточки WB'
                                                : !wbIds
                                                    ? 'у части выбора нет точного ID карточки WB'
                                                    : this.wbEditBusy
                                                        ? 'проверяем точный состав выбранных карточек…'
                                                        : 'откроется проверка изменений для выбранных карточек WB',
                            disabled: !chosen.length || !underLimit || !this.wbConnected ||
                                !this.wbAccountKnown || !workspaceCurrent || !wbIds || this.wbEditBusy,
                        },
                        {
                            key: 'wb_prices',
                            label: 'Изменить цены WB',
                            hint: !underLimit
                                ? 'за один запуск можно выбрать не больше 200 товаров'
                                : !wbIds
                                    ? chosen.some(function (item) { return !item.wb; })
                                        ? 'выберите только товары с карточкой Wildberries'
                                        : 'у части выбора нет связанного ID карточки WB'
                                    : !this.wbConnected
                                        ? 'подключите кабинет Wildberries'
                                        : 'откроется проверка выбранного набора цен',
                            disabled: !underLimit || !wbIds || !this.wbConnected,
                        },
                        this.ozonEnabled ? {
                            key: 'ozon_prices',
                            label: 'Изменить цены Ozon',
                            hint: ozonPriceResult.choices.length
                                ? 'откроется сравнение и ценовые заявки для одного магазина со всеми выбранными карточками'
                                : ozonPriceHint,
                            disabled: !chosen.length || !ozonPriceResult.choices.length,
                        } : null,
                        this.ozonEnabled ? {
                            key: 'ozon_workspace_update',
                            label: 'Изменить карточки Ozon',
                            hint: ozonUpdateHint,
                            disabled: ozonUpdateDisabled,
                        } : null,
                    ].filter(Boolean).map(function (action) {
                        return selectionReady ? action : Object.assign({}, action, {
                            disabled: true,
                            hint: this.selected.length ? selectionHint : action.hint,
                        });
                    }, this),
                }];
            },
            bulkIntentGroups: function () {
                var items = (this.actionGroups || []).reduce(function (all, group) {
                    return all.concat(group.items || []);
                }, []);
                var definitions = [
                    {
                        key: 'cards', label: 'Карточки', keys: ['common_content', 'wb_edit', 'ozon_workspace_update'],
                        labels: {
                            common_content: 'Данные товара',
                            wb_edit: 'Карточки WB',
                            ozon_workspace_update: 'Карточки Ozon',
                        },
                        emptyHint: this.selected.length > MAX_BULK_SELECTION
                            ? 'За один раз можно выбрать не больше 200 товаров.'
                            : this.selected.length > 50
                            ? 'Для изменения данных товара выберите не больше 50 товаров; для WB и Ozon нужны точные связанные карточки.'
                                : 'Выберите до 50 товаров для изменения данных Seller Hub или товары со связанными карточками WB/Ozon.',
                    },
                    {
                        key: 'prices', label: 'Цены', keys: ['wb_prices', 'ozon_prices'],
                        labels: { wb_prices: 'Цены WB', ozon_prices: 'Ценовые заявки Ozon' },
                        emptyHint: this.selected.length > MAX_BULK_SELECTION
                            ? 'За один раз можно выбрать не больше 200 товаров.'
                            : this.selected.length > MAX_OZON_PRICE_HANDOFF
                                ? 'Для Ozon доступно до 100 товаров; для WB нужны карточки с точной связью.'
                                : 'Нужны связанные карточки WB или доступный набор в одном магазине Ozon.',
                    },
                    {
                        key: 'create', label: 'Создать', keys: ['push_wb', 'ozon'],
                        labels: { push_wb: 'Создать карточки WB', ozon: 'Подготовить для Ozon' },
                        emptyHint: this.selected.length > MAX_BULK_SELECTION
                            ? 'За один раз можно выбрать не больше 200 товаров.'
                            : 'Для WB нужны товары без карточки; для Ozon — активный магазин и доступная подготовка.',
                    },
                    {
                        key: 'refresh', label: 'Обновить', keys: ['refresh_supplier'],
                        labels: { refresh_supplier: 'Из поставщика' },
                        emptyHint: 'За один раз можно обновить не больше 200 товаров.',
                    },
                ];
                return definitions.map(function (definition) {
                    var actions = definition.keys.map(function (key) {
                        return items.find(function (action) { return action.key === key; });
                    }).filter(function (action) { return action && !action.disabled; })
                        .map(function (action) {
                            return Object.assign({}, action, {
                                shortLabel: definition.labels[action.key] || action.label,
                            });
                        });
                    return Object.assign({}, definition, { actions: actions });
                });
            },
            verifiedSignedImportBatch: function () {
                return this.importBatchMode && this.importBatchKind === 'import' &&
                    !this.supplierRefreshBatchMode && !this.supplierRefreshResultBatchMode &&
                    !this.importBatchInvalid &&
                    typeof this.workspaceOwner === 'string' && this.workspaceOwner.length > 0 &&
                    typeof this.importBatch === 'string' && SIGNED_IMPORT_BATCH.test(this.importBatch) &&
                    this.importBatchSelectionVerified && !this.loading && !this.loadingMore &&
                    this.itemsScope === this.feedScopeKey();
            },
            inlineSupplierImportBatch: function () {
                return this.verifiedSignedImportBatch &&
                    !!this.inlineSupplierImportToken && this.inlineSupplierImportToken === this.importBatch;
            },
            wbCreatePendingForCurrentOwner: function () {
                return !!this.wbCreatePending && this.wbCreatePending.owner === this.workspaceOwner;
            },
            wbCreatePendingForCurrentBatch: function () {
                return this.wbCreatePendingForCurrentOwner && this.importBatchMode &&
                    this.importBatchKind === 'import' && this.importBatch === this.wbCreatePending.import_batch;
            },
            wbCreateReceiptVisible: function () {
                return this.wbCreateStorageProblem || this.wbCreatePendingForCurrentOwner ||
                    !!this.wbCreateError || !!this.wbCreateTerminalResult;
            },
            wbCreateReceiptState: function () {
                if (this.wbCreateStorageProblem) return 'blocked';
                return this.wbCreatePending ? this.wbCreatePending.state :
                    this.wbCreateTerminalResult ? 'terminal_result' : '';
            },
            wbCreateRetryAvailable: function () {
                return !!(this.wbCreateRetrySameKey && !this.wbCreateStorageProblem &&
                    !this.wbCreateBusy && !this.wbCreateReading &&
                    this.wbCreatePendingForCurrentBatch && this.importBatchSelectionVerified &&
                    !this.wbCreatePending.job_uid &&
                    this.itemsScope === this.feedScopeKey() &&
                    this.wbCreatePending.product_ids.every((id) => this.items.some((item) => item.id === id)));
            },
            wbCreateVerifyFeedAvailable: function () {
                return !!(this.wbCreatePending && this.wbCreatePending.state === 'terminal_refresh' &&
                    this.wbCreatePending.worker_exited === true && this.wbCreatePendingForCurrentBatch && !this.loading);
            },
            wbCreateBatchReturnUrl: function () {
                if (!this.wbCreatePendingForCurrentOwner) return '';
                if (typeof this.urls.workspace !== 'string' || !this.urls.workspace ||
                    this.urls.workspace.length > 2000 || !window.location) return '';
                try {
                    var target = new URL(this.urls.workspace, window.location.origin);
                    if (target.origin !== window.location.origin || target.username || target.password ||
                        target.search || target.hash || target.pathname !== window.location.pathname) return '';
                    return target.pathname + '?import_batch=' + encodeURIComponent(this.wbCreatePending.import_batch);
                } catch (_) { return ''; }
            },
            wbCreateOpenBatchAvailable: function () {
                return !!(this.wbCreateBatchReturnUrl &&
                    (!this.wbCreatePendingForCurrentBatch || !this.importBatchSelectionVerified));
            },
            ozonInlineReceiptVisible: function () {
                var receipt = this.ozonInlineReceipt;
                return !!receipt && receipt.owner === this.workspaceOwner &&
                    !!this.inlineSupplierImportToken && receipt.import_batch === this.inlineSupplierImportToken;
            },
            ozonInlineReceiptUrl: function () {
                return this.ozonInlineReceiptVisible
                    ? ozonRunLocation(this.urls.ozonUploads, this.ozonInlineReceipt.latest.job_uid) : '';
            },
            ozonInlineReceiptHasScope: function () {
                return !!(this.ozonInlineReceiptVisible && this.ozonInlineReceipt.latest.product_ids.length);
            },
            supplierImportOutcomeIsCompact: function () {
                var result = this.supplierImportOutcome;
                return !!result && result.imported > 0 && !result.skipped && !result.errors &&
                    result.drafts_status === 'complete' && !result.drafts_created && !result.draft_errors;
            },
            showSupplierImportNextSteps: function () {
                return this.inlineSupplierImportBatch && !this.selectAllLoading && this.selected.length > 0 &&
                    this.selectedItems.length === this.selected.length;
            },
            supplierImportNextActionGroup: function () {
                return this.bulkIntentGroups.find(function (row) { return row.key === 'create'; }) || null;
            },
            bulkIntentGroupsForDisplay: function () {
                return this.showSupplierImportNextSteps
                    ? this.bulkIntentGroups.filter(function (group) { return group.key !== 'create'; })
                    : this.bulkIntentGroups;
            },
            secondaryActions: function () {
                var chosen = this.selectedItems;
                var published = chosen.filter(function (item) { return item.wb; }).length;
                var wbIds = exactWbProductIds(chosen);
                var underLimit = chosen.length <= MAX_BULK_SELECTION;
                return [
                    {
                        key: 'audit',
                        label: 'Сверить с Wildberries',
                        hint: !underLimit
                            ? 'за один запуск можно выбрать не больше 200 товаров'
                            : !this.wbConnected
                                ? 'подключите кабинет Wildberries'
                                : !wbIds
                                ? chosen.some(function (item) { return !item.wb; })
                                    ? 'выберите только опубликованные карточки WB'
                                    : 'у части выбора нет связанного ID карточки WB'
                                : 'только чтение: покажет расхождения фото и характеристик',
                        disabled: !this.wbConnected || !wbIds || !underLimit,
                    },
                    {
                        key: 'enrich',
                        label: 'Дополнить карточки на WB',
                        hint: wbIds && underLimit
                            ? 'выбрать фото и характеристики на экране проверки'
                            : 'нужны только опубликованные карточки WB; лимит 200',
                        disabled: !wbIds || !underLimit,
                    },
                    {
                        key: 'delete',
                        label: 'Удалить из «Моих товаров»',
                        hint: published
                            ? published + ' ' + this.$fmt.plural(published, 'карточка WB останется', 'карточки WB останутся', 'карточек WB останутся') + ' в Wildberries'
                            : 'товары снова станут доступны для импорта',
                        danger: true,
                        disabled: !underLimit,
                    },
                ];
            },
            emptyState: function () {
                if (this.importBatchMode) {
                    return {
                        title: 'В этом импорте пока нет товаров',
                        text: 'В этой подборке нет доступных карточек для массовых действий.',
                        action: 'none',
                    };
                }
                if (this.supplierRefreshBatchMode) {
                    return {
                        title: 'Выбранный список недоступен',
                        text: 'Вернитесь к классическому списку и отметьте карточки заново.',
                        action: 'none',
                    };
                }
                if (this.filters.search || this.filters.supplier || this.filters.has_photos ||
                    this.filters.stock || this.filters.sort || this.filters.marketplace ||
                    this.filters.marketplace_state || this.filters.catalog_category ||
                    (this.activeTab && this.activeTab !== 'updates')) {
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
                var next = (value || '').trim().slice(0, 200);
                if (next !== this.filters.search) {
                    this.cancelSupplierRefreshReview({ restoreFocus: false });
                    this.cancelFilteredSelect();
                    this.cancelSelectionReview({ clearRows: true });
                }
                clearTimeout(this._searchTimer);
                this._searchTimer = setTimeout(function () {
                    if (next !== self.filters.search) self.filters.search = next;
                }, 300);
            },
            filters: { deep: true, handler: function () { this.load(); } },
            selected: {
                deep: true,
                handler: function (next) {
                    this.closeBulkActionPanel(false);
                    if (this.$refs.moreActions) this.$refs.moreActions.open = false;
                    if (this._wbEditHandoff &&
                        !sameOrderedIds(next, this._wbEditHandoff.selectionIds)) {
                        this.invalidateWbEditHandoff();
                    }
                    if (this._ozonWorkspaceUpdateHandoff &&
                        !sameOrderedIds(next, this._ozonWorkspaceUpdateHandoff.selectionIds)) {
                        this.invalidateOzonWorkspaceUpdate();
                    }
                    if (this.ozonWorkspaceUpdateReview &&
                        !sameOrderedIds(next, this.ozonWorkspaceUpdateReview.sourceIds)) {
                        this.cancelOzonWorkspaceUpdateReview({ restoreFocus: false });
                    }
                    if (this._selectAllAbort &&
                        !sameOrderedIds(next, this._selectAllSelectionSnapshot)) {
                        this.cancelFilteredSelect();
                    }
                    if (this._selectionReviewRequest &&
                        !sameOrderedIds(next, this._selectionReviewRequest.ids)) {
                        this.cancelSelectionReviewRequest();
                    }
                    if (validSelectionReviewIds(this.selectionReviewUnavailableIds) &&
                        !sameIds(next, this.selectionReviewUnavailableIds)) {
                        this.selectionReviewUnavailableIds = [];
                    }
                    var review = this.supplierRefreshReview;
                    if (review && review.phase !== 'applying' &&
                        !sameOrderedIds(next, review.selectionIds)) {
                        this.cancelSupplierRefreshReview({ restoreFocus: false });
                    }
                },
            },
            ozonError: function (value) {
                if (value) this.$nextTick(function () {
                    if (this.$refs.ozonDialog?.open) return;
                    this.$refs.ozonError?.focus();
                });
            },
        },

        mounted: function () {
            this._supplierRefreshUnmounted = false;
            this._supplierRefreshReturnAnchor = !!(window.location &&
                window.location.hash === '#supplier-refresh-result');
            if ((!this.importBatchMode && !this.supplierRefreshBatchMode) ||
                this.supplierRefreshResultBatchMode) {
                this.restoreSupplierRefreshResult();
            }
            if (workspaceReturnCapsule && !workspaceReturnScopeSeed) {
                this.consumeWorkspaceReturn(workspaceReturnCapsule.nonce);
            }
            this.load();
            if (!this.importBatchMode && !this.supplierRefreshBatchMode) {
                this.loadFacets();
                this.loadCatalogCategories();
            }
        },
        beforeUnmount: function () {
            this._mpUnmounted = true;
            this._catalogCategoriesRequestId = (this._catalogCategoriesRequestId || 0) + 1;
            if (this._catalogCategoriesAbort) this._catalogCategoriesAbort.abort();
            this._catalogCategoriesAbort = null;
            this.invalidateWbEditHandoff();
            this.invalidateOzonWorkspaceUpdate();
            this.stopJobTracking();
            this.stopWbCreateReceiptTracking();
            if (this._wbCreatePostTimer) clearTimeout(this._wbCreatePostTimer);
            this._wbCreatePostTimer = null;
            if (this._wbCreatePostAbort) this._wbCreatePostAbort.abort();
            this._wbCreatePostAbort = null;
            if (this._abort) this._abort.abort();
            if (this._loadMoreAbort) this._loadMoreAbort.abort();
            this.cancelSupplierRefreshRestore();
            this._supplierRefreshUnmounted = true;
            this.cancelSupplierRefreshReview({ restoreFocus: false, force: true });
            this.cancelFilteredSelect();
            this.cancelSelectionReviewRequest();
        },

        methods: {
            selectionItemsForIds: function (ids) {
                if (!validSelectionReviewIds(ids)) return [];
                var byId = new Map();
                (this.items || []).forEach(function (item) {
                    if (item && Number.isSafeInteger(item.id) && item.id > 0 && !byId.has(item.id)) {
                        byId.set(item.id, item);
                    }
                });
                (this.selectionReviewRows || []).forEach(function (item) {
                    if (item && Number.isSafeInteger(item.id) && item.id > 0) byId.set(item.id, item);
                });
                var result = ids.map(function (id) { return byId.get(id); });
                return result.some(function (item) { return !item; }) ? [] : result;
            },
            cancelSelectionReviewRequest: function () {
                var request = this._selectionReviewRequest;
                this._selectionReviewRequest = null;
                this._selectionReviewRevision = (this._selectionReviewRevision || 0) + 1;
                if (request && request.abort) request.abort.abort();
                if (request && typeof request.cancel === 'function') request.cancel();
                this.selectionReviewLoading = false;
            },
            cancelSelectionReview: function (options) {
                options = options || {};
                this.cancelSelectionReviewRequest();
                this.selectionReviewActive = false;
                this.selectionReviewError = '';
                if (options.clearRows) {
                    this.selectionReviewRows = [];
                    this.selectionReviewIds = [];
                }
            },
            selectionReviewRequestIsCurrent: function (request) {
                return !!request && this._selectionReviewRequest === request &&
                    request.abort && (!request.abort.signal.aborted || request.timedOut) &&
                    this._selectionReviewRevision === request.revision &&
                    this._feedRevision === request.feedRevision &&
                    this.feedScopeKey() === request.scopeKey &&
                    this.itemsScope === request.scopeKey &&
                    sameOrderedIds(this.selected, request.ids) &&
                    (this.searchInput || '').trim().slice(0, 200) === this.filters.search &&
                    (!request.restore || (this.workspaceReturnPending &&
                        this.workspaceSelectionReviewReturnMatches(request.capsule))) &&
                    !this.importBatchMode && !this.supplierRefreshBatchMode;
            },
            validSelectionReviewResponse: function (data, expectedIds) {
                var responseIds = data && orderedProductIds(data.items);
                return !!data && typeof data === 'object' && !Array.isArray(data) &&
                    data.success === true && Array.isArray(data.items) &&
                    validSelectionReviewIds(expectedIds) && responseIds &&
                    sameOrderedIds(responseIds, expectedIds);
            },
            readSelectionReviewRows: function (ids, options) {
                options = options || {};
                if (!validSelectionReviewIds(ids) || typeof this.urls.selection !== 'string' ||
                    !this.urls.selection) return Promise.resolve({ ok: false, invalid: true });
                this.cancelSelectionReviewRequest();
                var request = {
                    ids: ids.slice(),
                    scopeKey: this.feedScopeKey(),
                    feedRevision: this._feedRevision || 0,
                    revision: (this._selectionReviewRevision || 0) + 1,
                    abort: new AbortController(),
                    restore: options.restore === true,
                    capsule: options.capsule || null,
                };
                this._selectionReviewRevision = request.revision;
                this._selectionReviewRequest = request;
                this.selectionReviewActive = false;
                this.selectionReviewRows = [];
                this.selectionReviewIds = [];
                this.selectionReviewError = '';
                this.selectionReviewLoading = true;
                var result;
                try {
                    result = this.$api.post(this.urls.selection, { ids: request.ids.slice() }, {
                        signal: request.abort.signal,
                    });
                } catch (err) {
                    result = Promise.reject(err);
                }
                var self = this;
                var timeoutId = null;
                var cancelledResult = { selectionReviewCancelled: true };
                var guard = new Promise(function (resolve, reject) {
                    request.cancel = function () { resolve(cancelledResult); };
                    timeoutId = setTimeout(function () {
                        request.timedOut = true;
                        var timeoutError = new Error('Selection read timed out');
                        timeoutError.name = 'SelectionReviewTimeoutError';
                        reject(timeoutError);
                        request.abort.abort();
                    }, 12000);
                });
                return Promise.race([Promise.resolve(result), guard]).then(function (data) {
                    if (data === cancelledResult) return { ok: false, stale: true };
                    if (!self.selectionReviewRequestIsCurrent(request)) return { ok: false, stale: true };
                    if (!self.validSelectionReviewResponse(data, request.ids) ||
                        (request.restore && !self.workspaceReturnSelectionMatches(data.items, request.ids))) {
                        self.selectionReviewUnavailableIds = request.ids.slice();
                        self.selectionReviewError = 'Не удалось подтвердить точный список выбранных товаров. Повторите чтение.';
                        return { ok: false, mismatch: true };
                    }
                    self.selectionReviewRows = data.items;
                    self.selectionReviewIds = request.ids.slice();
                    self.selectionReviewUnavailableIds = [];
                    if (options.activate === true) {
                        self.selectionReviewActive = true;
                        self.$nextTick(function () {
                            self.$refs.selectionReviewHeading?.focus({ preventScroll: true });
                        });
                    }
                    return { ok: true, items: data.items };
                }).catch(function (err) {
                    if (!self.selectionReviewRequestIsCurrent(request) ||
                        (err && err.name === 'AbortError')) return { ok: false, stale: true };
                    self.selectionReviewUnavailableIds = request.ids.slice();
                    self.selectionReviewError = err && err.name === 'SelectionReviewTimeoutError'
                        ? 'Чтение заняло слишком много времени. Повторите попытку.'
                        : err && err.status === 404
                        ? 'Часть выбранных товаров больше недоступна. Обновите список и выберите товары снова.'
                        : err && err.status === 409
                            ? 'Состав товаров изменился. Повторите чтение выбранного списка.'
                            : 'Не удалось прочитать точный список выбранных товаров. Повторите попытку.';
                    return { ok: false, status: err && err.status };
                }).finally(function () {
                    if (timeoutId !== null) clearTimeout(timeoutId);
                    if (self._selectionReviewRequest === request) {
                        self._selectionReviewRequest = null;
                        self.selectionReviewLoading = false;
                    }
                });
            },
            showSelected: function () {
                if (!this.selectionReviewAvailable || !validSelectionReviewIds(this.selected)) return false;
                this.error = '';
                var ids = this.selected.slice();
                return this.readSelectionReviewRows(ids, { activate: true }).then(function (result) {
                    return !!(result && result.ok);
                });
            },
            focusCatalogAfterSelectionReview: function () {
                var list = this.$refs.catalogListFocus;
                if (list && typeof list.focus === 'function') {
                    list.focus({ preventScroll: true });
                    return true;
                }
                var filter = this.$refs.catalogSearch;
                var search = filter && filter.$refs && filter.$refs.search;
                if (search && typeof search.focus === 'function') {
                    search.focus({ preventScroll: true });
                    return true;
                }
                return false;
            },
            returnToCatalog: function () {
                this.selectionReviewActive = false;
                this.selectionReviewError = '';
                this.$nextTick(function () {
                    this.focusCatalogAfterSelectionReview();
                });
            },
            onProductSelectionChange: function (id, event) {
                var checkbox = event && event.currentTarget;
                var checked = !!(checkbox && checkbox.checked);
                if (!Number.isSafeInteger(id) || id <= 0 || this.loading || this.loadingMore ||
                    this.selectionReviewLoading || this.itemsScope !== this.feedScopeKey() ||
                    !this.visibleItems.some(function (item) { return item && item.id === id; })) {
                    if (checkbox) checkbox.checked = this.selected.indexOf(id) !== -1;
                    return false;
                }
                var next = this.selected.slice();
                var currentIndex = next.indexOf(id);
                if (checked && currentIndex === -1) {
                    if (next.length >= MAX_BULK_SELECTION) {
                        checkbox.checked = false;
                        this.error = 'За один раз можно выбрать не больше 200 товаров. Снимите часть отметок и повторите.';
                        return false;
                    }
                    next.push(id);
                } else if (!checked && currentIndex !== -1) {
                    next.splice(currentIndex, 1);
                }
                this.selected = next;
                this.markWbCreateSelectionIntent();
                return true;
            },
            openSupplierImport: function () {
                if (!this.importPanelAvailable) return false;
                this.closeBulkActionPanel(false);
                if (this.$refs.moreActions) this.$refs.moreActions.open = false;
                if (this.$refs.headMore) this.$refs.headMore.open = false;
                this._importSelectionBeforeOpen = this.selected.slice();
                this.supplierImportOpen = true;
                return true;
            },
            closeSupplierImport: function () {
                this.supplierImportOpen = false;
                if (Array.isArray(this._importSelectionBeforeOpen)) {
                    this.selected = this._importSelectionBeforeOpen.slice();
                }
                this._importSelectionBeforeOpen = null;
            },
            handleSupplierImportSuccess: function (result) {
                this.supplierImportOutcome = result;
                this.supplierImportFallbackUrl = '';
                this.importBatchSelectionVerified = false;
                this.supplierImportOpen = false;
                this.selected = [];
                this.closeBulkActionPanel(false);
                if (this.$refs.moreActions) this.$refs.moreActions.open = false;
                this._importSelectionBeforeOpen = null;
                if (!result || !Number.isSafeInteger(result.imported) || result.imported < 1) {
                    this.importBatchSelectionPending = false;
                    this.importBatchExpectedCount = 0;
                    this.selected = [];
                    return false;
                }
                var bundle = importBundle || {};
                var targetLocation = typeof bundle.canonicalBatchLocation === 'function'
                    ? bundle.canonicalBatchLocation(
                        result.batch_url, result.import_batch, this.urls.workspace,
                        window.location.origin,
                    ) : null;
                if (!targetLocation || result.imported > MAX_BULK_SELECTION) {
                    this.error = 'Импорт подтверждён, но точный список не прошёл проверку. Обновите эту страницу перед следующим действием.';
                    return false;
                }
                this.supplierImportFallbackUrl = targetLocation;
                if (!window.history || typeof window.history.replaceState !== 'function') {
                    this.error = 'Импорт подтверждён, но браузер не позволил обновить список без перезагрузки. Откройте точный список по ссылке.';
                    return false;
                }
                try {
                    if (this.workspaceReturnPending && workspaceReturnCapsule) {
                        this.consumeWorkspaceReturn(workspaceReturnCapsule.nonce);
                    }
                    var currentState = window.history.state && typeof window.history.state === 'object'
                        ? Object.assign({}, window.history.state) : {};
                    currentState.sellerHubImportBatch = {
                        token: result.import_batch,
                        kind: 'import',
                    };
                    currentState.sellerHubInlineImportBatch = {
                        v: 1,
                        owner: boot.workspaceOwner,
                        token: result.import_batch,
                    };
                    window.history.replaceState(currentState, '', targetLocation);
                } catch (_) {
                    this.error = 'Импорт подтверждён, но ссылку рабочего списка не удалось обновить. Откройте точный список по ссылке.';
                    return false;
                }
                this.supplierRefreshBatch = '';
                this.supplierRefreshBatchMode = false;
                this.supplierRefreshBatchInvalid = false;
                this.importBatch = result.import_batch;
                this.importBatchMode = true;
                this.importBatchKind = 'import';
                this.inlineSupplierImportToken = result.import_batch;
                this.importBatchInvalid = false;
                this.supplierRefreshResultBatchMode = false;
                this.supplierRefreshSelectPending = false;
                this.importBatchExpectedCount = result.imported;
                this.importBatchSelectionPending = true;
                this.activeTab = '';
                this.searchInput = '';
                this.filters = {
                    search: '', supplier: '', has_photos: '', stock: '', sort: '',
                    marketplace: '', marketplace_state: '', catalog_category: '',
                };
                this.selected = [];
                this.error = '';
                this.load();
                return true;
            },
            clearImportBatch: function () {
                var target;
                try {
                    if (typeof this.urls.workspace !== 'string' || !this.urls.workspace) throw new Error('missing URL');
                    target = new URL(this.urls.workspace, window.location.origin);
                    if (target.origin !== window.location.origin || target.username || target.password ||
                        target.hash || target.pathname !== window.location.pathname ||
                        !window.history || typeof window.history.replaceState !== 'function') {
                        throw new Error('unsafe workspace URL');
                    }
                    target.search = '';
                    var currentState = window.history.state && typeof window.history.state === 'object'
                        ? Object.assign({}, window.history.state) : {};
                    delete currentState.sellerHubImportBatch;
                    delete currentState.sellerHubInlineImportBatch;
                    window.history.replaceState(currentState, '', target.pathname);
                } catch (_) {
                    this.error = 'Не удалось вернуться к общему списку без перезагрузки. Текущий выбор сохранён.';
                    return false;
                }
                this.importBatch = '';
                this.importBatchMode = false;
                this.importBatchKind = 'import';
                this.inlineSupplierImportToken = '';
                this.importBatchInvalid = false;
                this.supplierRefreshResultBatchMode = false;
                this.supplierRefreshSelectPending = false;
                this.importBatchSelectionPending = false;
                this.importBatchExpectedCount = 0;
                this.importBatchSelectionVerified = false;
                this.selected = [];
                this.activeTab = '';
                this.searchInput = '';
                this.filters = {
                    search: '', supplier: '', has_photos: '', stock: '', sort: '',
                    marketplace: '', marketplace_state: '', catalog_category: '',
                };
                this.error = '';
                // Replacing filters triggers the deep watcher, which performs
                // the one ordinary feed read after the signed batch is cleared.
                // Calling load here as well starts a duplicate request.
                this.loadFacets();
                return true;
            },
            closeBulkActionPanel: function (restoreFocus) {
                if (!this.activeBulkActionGroup) return;
                this.activeBulkActionGroup = '';
                var trigger = this._bulkActionTrigger;
                this._bulkActionTrigger = null;
                if (restoreFocus && trigger && trigger.isConnected) {
                    this.$nextTick(function () {
                        if (trigger.isConnected) trigger.focus();
                    });
                }
            },
            priceWorkspaceItemsBySelection: function (selectionIds) {
                if (!validSelectionReviewIds(selectionIds)) return null;
                var selectedItems = this.selectionItemsForIds(selectionIds);
                if (!selectedItems.length || selectedItems.length !== selectionIds.length) return null;
                var currentIds = orderedProductIds(this.items) || [];
                var reviewIds = this.selectionReviewIds;
                if (selectionIds.some(function (id) {
                    return currentIds.indexOf(id) === -1 && reviewIds.indexOf(id) === -1;
                })) return null;
                return selectedItems;
            },
            priceWorkspaceLaunchIsCurrent: function (launch) {
                if (!launch || this.loading || this.loadingMore || this.selectAllLoading ||
                    this._feedRevision !== launch.feedRevision || this.feedScopeKey() !== launch.scopeKey ||
                    this.itemsScope !== launch.scopeKey ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) return false;
                if (launch.source === 'bulk' && (!sameOrderedIds(this.selected, launch.localSelectionIds) ||
                    !workspaceReturnScopeMatchesCurrent(this, launch.returnScope))) return false;
                if (launch.source === 'row' && launch.localSelectionIds.length !== 1) return false;
                var sourceItems = this.priceWorkspaceItemsBySelection(launch.localSelectionIds);
                if (!sourceItems) return false;
                if (launch.marketplace === 'wb') {
                    return this.wbConnected && this.wbAccountKnown &&
                        this.wbAccountId === launch.wbAccountId &&
                        sameOrderedIds(orderedWbProductIds(sourceItems, launch.localSelectionIds), launch.productIds);
                }
                if (launch.marketplace !== 'ozon') return false;
                var accountResult = this.resolveOzonPriceAccountChoices(sourceItems, launch.localSelectionIds);
                var freshChoice = accountResult.choices.find(function (choice) {
                    return choice.account_id === launch.accountId;
                });
                return !!freshChoice && sameOrderedIds(freshChoice.listing_ids, launch.productIds);
            },
            openPriceWorkspaceDialog: function (marketplace, localSelectionIds, options) {
                options = options || {};
                if (!this.priceWorkspaceDialogAvailable) return false;
                if (this.priceWorkspaceDialogOpen) return false;
                var source = options.source === 'row' ? 'row' : 'bulk';
                if (this.loading || this.loadingMore || this.selectAllLoading ||
                    this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search ||
                    !Array.isArray(localSelectionIds) || !localSelectionIds.length ||
                    new Set(localSelectionIds).size !== localSelectionIds.length ||
                    (source === 'bulk' && (!this.selectionRowsVerified ||
                        !sameOrderedIds(this.selected, localSelectionIds))) ||
                    (source === 'row' && localSelectionIds.length !== 1)) {
                    this.error = 'Список или выбор изменился. Обновите список и отметьте товары заново.';
                    return false;
                }
                var sourceItems = this.priceWorkspaceItemsBySelection(localSelectionIds);
                if (!sourceItems) {
                    this.error = 'Не удалось проверить точный список товаров. Обновите страницу и повторите действие.';
                    return false;
                }
                var productIds = null;
                var accountId = null;
                var accountLabel = '';
                if (marketplace === 'wb') {
                    productIds = orderedWbProductIds(sourceItems, localSelectionIds);
                    if (!productIds || productIds.length > MAX_BULK_SELECTION ||
                        !this.wbConnected || !this.wbAccountKnown) {
                        this.error = 'Для изменения цены выберите точные карточки Wildberries и проверьте подключение кабинета.';
                        return false;
                    }
                    accountLabel = 'Wildberries';
                } else if (marketplace === 'ozon') {
                    var choice = options.choice;
                    var choices = this.resolveOzonPriceAccountChoices(sourceItems, localSelectionIds).choices;
                    var freshChoice = choice && choices.find(function (candidate) {
                        return candidate.account_id === choice.account_id;
                    });
                    if (!freshChoice || !sameOrderedIds(freshChoice.listing_ids, choice.listing_ids)) {
                        this.error = 'Связанные карточки Ozon изменились. Обновите список и повторите действие.';
                        return false;
                    }
                    productIds = freshChoice.listing_ids.slice();
                    accountId = freshChoice.account_id;
                    accountLabel = freshChoice.account_label || ('Магазин № ' + accountId);
                } else {
                    return false;
                }
                var returnScope = source === 'bulk' ? this.buildWorkspaceReturnScope() : null;
                var rows = sourceItems.map(function (item, index) {
                    return {
                        id: productIds[index],
                        title: typeof item.title === 'string' ? item.title : '',
                        article: typeof item.vendor_code === 'string' ? item.vendor_code : '',
                        photo: typeof item.photo === 'string' ? item.photo : '',
                    };
                });
                var scope = {
                    marketplace: marketplace,
                    productIds: productIds.slice(),
                    accountId: accountId,
                    rows: rows,
                    accountLabel: accountLabel,
                };
                var launch = {
                    source: source,
                    marketplace: marketplace,
                    localSelectionIds: localSelectionIds.slice(),
                    productIds: productIds.slice(),
                    accountId: accountId,
                    accountLabel: accountLabel,
                    wbAccountId: this.wbAccountId,
                    scopeKey: this.feedScopeKey(),
                    feedRevision: this._feedRevision,
                    returnScope: returnScope,
                    priceTarget: {channel: marketplace, ids: productIds.slice(), accountId: accountId},
                };
                if (!this.priceWorkspaceLaunchIsCurrent(launch)) {
                    this.error = 'Состав выбора или связь с маркетплейсом изменилась. Обновите список и повторите действие.';
                    return false;
                }
                this.priceWorkspaceLaunch = launch;
                this.priceWorkspaceScope = scope;
                this.priceWorkspaceReviewHref = '';
                this.priceWorkspaceReturnFocus = options.returnFocus || null;
                this.priceWorkspaceDialogOpen = true;
                this.error = '';
                return true;
            },
            closePriceWorkspaceDialog: function () {
                this.priceWorkspaceDialogOpen = false;
                this.priceWorkspaceScope = null;
                this.priceWorkspaceLaunch = null;
                this.priceWorkspaceReviewHref = '';
                var trigger = this.priceWorkspaceReturnFocus;
                this.priceWorkspaceReturnFocus = null;
                this.$nextTick(function () {
                    if (trigger && trigger.isConnected) trigger.focus();
                });
            },
            priceWorkspaceReceiptHref: function (launch, receipt) {
                if (!launch || !receipt || !receipt.destination ||
                    typeof receipt.destination.href !== 'string') return '';
                var href = receipt.destination.href;
                if (!href || href.length > 4096 || /[\u0000-\u001f\u007f]/.test(href)) return '';
                try {
                    var target = new URL(href, window.location.origin);
                    if (target.origin !== window.location.origin || target.username || target.password ||
                        target.hash || href !== target.pathname + target.search) return '';
                    if (launch.marketplace === 'wb') {
                        if (!Number.isSafeInteger(receipt.batchId) || receipt.batchId <= 0) return '';
                        var suffix = receipt.status === 'pending_review' ? '/confirm' : '';
                        var expected = '/prices/batch/' + receipt.batchId + suffix;
                        return receipt.redirect === expected && href === expected ? href : '';
                    }
                    if (launch.marketplace === 'ozon' && receipt.marketplace === 'ozon' &&
                        receipt.accountId === launch.accountId &&
                        target.pathname === '/marketplaces/commercial/') {
                        return href;
                    }
                } catch (_) {}
                return '';
            },
            handlePriceWorkspaceSaved: function (receipt) {
                this.priceWorkspaceReviewHref = '';
                var launch = this.priceWorkspaceLaunch;
                if (!launch || launch.source !== 'bulk' ||
                    !this.priceWorkspaceLaunchIsCurrent(launch) || !launch.returnScope) return false;
                if (!receipt || receipt.marketplace !== launch.marketplace ||
                    receipt.selectedCount !== launch.productIds.length) return false;
                var destination = this.priceWorkspaceReceiptHref(launch, receipt);
                if (!destination) return false;
                var priceResult = null;
                if (launch.marketplace === 'wb') {
                    if (!Number.isSafeInteger(receipt.changedCount) || receipt.changedCount < 0 ||
                        receipt.changedCount > receipt.selectedCount) return false;
                    priceResult = {channel: 'wb', batchId: receipt.batchId};
                } else {
                    var proposalIds = receipt.proposalIds;
                    if (!Array.isArray(proposalIds) || !proposalIds.length ||
                        proposalIds.length > launch.productIds.length ||
                        receipt.savedCount !== proposalIds.length || receipt.reviewUrl !== destination ||
                        proposalIds.some(function (id) { return !Number.isSafeInteger(id) || id <= 0; }) ||
                        new Set(proposalIds).size !== proposalIds.length) return false;
                    priceResult = {
                        channel: 'ozon',
                        accountId: launch.accountId,
                        proposalIds: proposalIds.slice(),
                    };
                }
                var nonce = storePriceWorkspaceReturn(
                    this, launch.priceTarget, launch.localSelectionIds, launch.returnScope,
                );
                var hash = workspaceReturnHash(nonce);
                var helpers = window.SellerHubCommonContent || {};
                if (!nonce || !hash || typeof helpers.bindPriceWorkspaceResult !== 'function') {
                    if (nonce) this.consumeWorkspaceReturn(nonce);
                    return false;
                }
                var bound = '';
                try {
                    bound = helpers.bindPriceWorkspaceResult(hash, {
                        owner: this.workspaceOwner,
                        priceTarget: launch.priceTarget,
                        priceResult: priceResult,
                    });
                } catch (_) {}
                if (bound !== hash) {
                    this.consumeWorkspaceReturn(nonce);
                    return false;
                }
                this.priceWorkspaceReviewHref = destination + hash;
                return true;
            },
            openLegacyPriceWorkspace: function () {
                var launch = this.priceWorkspaceLaunch;
                if (!this.priceWorkspaceLaunchIsCurrent(launch)) {
                    this.error = 'Состав выбора или связь с маркетплейсом изменились. Обновите список и повторите действие.';
                    this.closePriceWorkspaceDialog();
                    return false;
                }
                var nonce = null;
                if (launch.source === 'bulk' && launch.returnScope) {
                    nonce = storePriceWorkspaceReturn(
                        this, launch.priceTarget, launch.localSelectionIds, launch.returnScope,
                    );
                }
                var hash = workspaceReturnHash(nonce);
                try {
                    var base = launch.marketplace === 'wb' ? this.urls.wbPriceChange : this.urls.ozonCommercial;
                    if (typeof base !== 'string' || !base) throw new Error('missing URL');
                    var target = new URL(base, window.location.origin);
                    if (target.origin !== window.location.origin || target.username || target.password) {
                        throw new Error('foreign URL');
                    }
                    if (launch.marketplace === 'wb') {
                        if (target.pathname !== '/prices/change') throw new Error('unexpected WB price route');
                        target.search = '';
                        target.searchParams.set('product_ids', launch.productIds.join(','));
                    } else {
                        if (target.pathname.indexOf('/marketplaces/commercial') !== 0) {
                            throw new Error('unexpected Ozon price route');
                        }
                        target.searchParams.set('account_id', String(launch.accountId));
                        target.searchParams.set('bulk_listing_ids', launch.productIds.join(','));
                    }
                    target.hash = hash.replace(/^#/, '');
                    window.location.assign(target.pathname + target.search + target.hash);
                    return true;
                } catch (_) {
                    if (nonce) this.consumeWorkspaceReturn(nonce);
                    this.error = 'Не удалось открыть обычный редактор цен. Повторите действие со свежим выбором.';
                    return false;
                }
            },
            canOpenRowWbPrice: function (item) {
                return !!(this.priceWorkspaceDialogAvailable && !this.loading && !this.loadingMore &&
                    !this.selectAllLoading && this.itemsScope === this.feedScopeKey() &&
                    this.wbConnected && this.wbAccountKnown && exactWbProductIds([item]));
            },
            canOpenRowOzonPrice: function (item) {
                if (!this.priceWorkspaceDialogAvailable || this.loading || this.loadingMore ||
                    this.selectAllLoading || this.itemsScope !== this.feedScopeKey() ||
                    !this.ozonEnabled || !item || !Number.isSafeInteger(item.id)) return false;
                return this.resolveOzonPriceAccountChoices([item], [item.id]).choices.length > 0;
            },
            openRowWbPrice: function (item, event) {
                if (!this.canOpenRowWbPrice(item)) return false;
                return this.openPriceWorkspaceDialog('wb', [item.id], {
                    source: 'row',
                    returnFocus: event && event.currentTarget,
                });
            },
            openRowOzonPrice: function (item, event) {
                if (!this.canOpenRowOzonPrice(item)) return false;
                return this.openOzonPriceHandoff([item], [item.id], {
                    source: 'row',
                    returnFocus: event && event.currentTarget,
                });
            },
            activateBulkIntent: function (group, event) {
                var details = this.$refs.moreActions;
                if (details) details.open = false;
                var headMore = this.$refs.headMore;
                if (headMore) headMore.open = false;
                if (group.actions.length === 1) {
                    var directKey = group.actions[0].key;
                    var returnFocus = ['ozon', 'wb_prices', 'ozon_prices'].includes(directKey)
                        ? event && event.currentTarget : null;
                    this.closeBulkActionPanel(false);
                    this.startAction(group.actions[0], returnFocus);
                    return;
                }
                if (this.activeBulkActionGroup === group.key) {
                    this.closeBulkActionPanel(false);
                    return;
                }
                this.activeBulkActionGroup = group.key;
                this._bulkActionTrigger = event && event.currentTarget;
            },
            runBulkIntentAction: function (action, event) {
                var key = action && action.key;
                var returnFocus = ['wb_prices', 'ozon_prices'].includes(key)
                    ? this._bulkActionTrigger || (event && event.currentTarget)
                    : key === 'ozon'
                        ? (event && event.currentTarget) || this._bulkActionTrigger : null;
                this.closeBulkActionPanel(false);
                this.startAction(action, returnFocus);
            },
            onMoreActionsToggle: function (event) {
                if (event && event.currentTarget && event.currentTarget.open) {
                    this.closeBulkActionPanel(false);
                    if (this.$refs.headMore) this.$refs.headMore.open = false;
                }
            },
            onHeadMoreToggle: function (event) {
                if (event && event.currentTarget && event.currentTarget.open) {
                    this.closeBulkActionPanel(false);
                    if (this.$refs.moreActions) this.$refs.moreActions.open = false;
                }
            },
            invalidateWbEditHandoff: function (expectedHandoff) {
                var handoff = this._wbEditHandoff;
                if (!handoff || (expectedHandoff && handoff !== expectedHandoff)) return;
                this._wbEditHandoff = null;
                if (handoff.abort) handoff.abort.abort();
                if (handoff.returnNonce) this.consumeWorkspaceReturn(handoff.returnNonce);
                this.wbEditBusy = false;
            },
            invalidateOzonWorkspaceUpdate: function (expectedHandoff) {
                var handoff = this._ozonWorkspaceUpdateHandoff;
                if (!handoff || (expectedHandoff && handoff !== expectedHandoff)) return;
                this._ozonWorkspaceUpdateHandoff = null;
                if (handoff.abort) handoff.abort.abort();
                if (handoff.returnNonce) this.consumeWorkspaceReturn(handoff.returnNonce);
                this.ozonWorkspaceUpdateBusy = false;
            },
            selectUpdatedSupplierBatch: function () {
                var result = this.supplierRefreshResult;
                var token = result && result.updatedBatch;
                if (!result || !['complete', 'partial'].includes(result.kind) ||
                    !Number.isSafeInteger(result.updated) || result.updated < 1 ||
                    !validSupplierUpdatedBatchToken(token)) {
                    this.error = 'Не удалось подтвердить список обновлённых товаров. Массовый выбор недоступен.';
                    return false;
                }
                if (this.loading || this.loadingMore || this.selectAllLoading) {
                    this.error = 'Список обновляется. Дождитесь завершения загрузки и повторите выбор.';
                    return false;
                }
                var target;
                try {
                    if (typeof this.urls.workspace !== 'string' || !this.urls.workspace) throw new Error('missing URL');
                    target = new URL(this.urls.workspace, window.location.origin);
                    if (target.origin !== window.location.origin || target.username || target.password || target.hash) {
                        throw new Error('foreign workspace URL');
                    }
                    target.search = '';
                    target.searchParams.set('import_batch', token);
                    target.hash = 'supplier-refresh-result';
                    if (!window.history || typeof window.history.replaceState !== 'function') {
                        throw new Error('history unavailable');
                    }
                    window.history.replaceState(window.history.state || null, '', target.pathname + target.search + target.hash);
                } catch (_) {
                    this.error = 'Не удалось открыть точный список обновлённых товаров. Обновите страницу и повторите действие.';
                    return false;
                }
                this.supplierRefreshBatch = '';
                this.supplierRefreshBatchMode = false;
                this.supplierRefreshBatchInvalid = false;
                this.importBatch = token;
                this.importBatchMode = true;
                this.importBatchKind = 'supplier_refresh_result';
                this.importBatchInvalid = false;
                this.supplierRefreshResultBatchMode = true;
                this.supplierRefreshSelectPending = true;
                this.activeTab = '';
                this.searchInput = '';
                this.filters = {
                    search: '', supplier: '', has_photos: '', stock: '', sort: '',
                    marketplace: '', marketplace_state: '', catalog_category: '',
                };
                this.error = '';
                this.load();
                return true;
            },
            queryParams: function (page, perPage) {
                if (this.importBatchMode) {
                    return { import_batch: this.importBatch };
                }
                if (this.supplierRefreshBatchMode) {
                    return { supplier_refresh_batch: this.supplierRefreshBatch };
                }
                var params = {
                    search: this.filters.search,
                    supplier: this.filters.supplier,
                    has_photos: this.filters.has_photos,
                    stock: this.filters.stock,
                    sort: this.filters.sort,
                    page: page || 1,
                    per_page: perPage || 50,
                };
                if (this.filters.marketplace) params.marketplace = this.filters.marketplace;
                if (this.filters.marketplace_state) {
                    params.marketplace_state = this.filters.marketplace_state;
                }
                if (this.filters.catalog_category) {
                    params.catalog_category = this.filters.catalog_category;
                }
                if (this.activeTab === 'updates') params.updates = '1';
                else if (this.activeTab) params.status = this.activeTab;
                return params;
            },
            consumeWorkspaceReturn: function (nonce) {
                this.workspaceReturnPending = false;
                clearSelectionReviewReturn(nonce);
                if (workspaceSelectionReviewReturn && workspaceSelectionReviewReturn.nonce === nonce) {
                    workspaceSelectionReviewReturn = null;
                }
                if (typeof workspaceHelpers.consumeWorkspaceReturn === 'function') {
                    try { workspaceHelpers.consumeWorkspaceReturn(nonce); } catch (_) {}
                }
            },
            workspaceReturnChanged: function () {
                this.selected = [];
                this.cancelSelectionReview({ clearRows: true });
                this.workspaceReturnNote = workspaceChangedNote;
                if (workspaceReturnCapsule) this.consumeWorkspaceReturn(workspaceReturnCapsule.nonce);
            },
            workspaceSelectionReviewReturnMatches: function (capsule) {
                return !!capsule && !this.importBatchMode && !this.supplierRefreshBatchMode &&
                    !!workspaceSelectionReviewReturn &&
                    workspaceSelectionReviewReturn.owner === boot.workspaceOwner &&
                    workspaceSelectionReviewReturn.nonce === capsule.nonce &&
                    sameOrderedIds(workspaceSelectionReviewReturn.ids, capsule.selectionIds);
            },
            restoreWorkspaceSelectionReview: function (capsule, scope, feedRevision) {
                var ids = capsule && capsule.selectionIds;
                if (!this.workspaceSelectionReviewReturnMatches(capsule) ||
                    !validSelectionReviewIds(ids) || this.itemsScope !== scope ||
                    this.feedScopeKey() !== scope || this._feedRevision !== feedRevision) {
                    this.workspaceReturnChanged();
                    this.error = 'Не удалось восстановить точный выбранный список. Проверьте список и отметьте товары заново.';
                    return Promise.resolve(false);
                }
                this.selected = ids.slice();
                var self = this;
                return this.readSelectionReviewRows(ids, {
                    activate: true,
                    restore: true,
                    capsule: capsule,
                }).then(function (result) {
                    if (!result.ok && !result.stale) {
                        self.workspaceReturnChanged();
                        self.error = 'Не удалось восстановить точный выбранный список. Проверьте список и отметьте товары заново.';
                    }
                    return !!result.ok;
                });
            },
            workspaceReturnScopeMatches: function (scope) {
                return this.importBatchMode
                    ? workspaceReturnCapsule && workspaceReturnCapsule.batch === this.importBatch
                    : scope && this.activeTab === scope.activeTab &&
                        this.filters.search === scope.search &&
                        this.filters.supplier === scope.supplier &&
                        this.filters.has_photos === scope.has_photos &&
                        this.filters.stock === scope.stock &&
                        this.filters.sort === scope.sort &&
                        (this.filters.marketplace || '') === (scope.marketplace || '') &&
                        (this.filters.marketplace_state || '') === (scope.marketplace_state || '') &&
                        (this.filters.catalog_category || '') === (scope.catalog_category || '') &&
                        (this.searchInput || '').trim().slice(0, 200) === scope.search;
            },
            workspaceReturnScopeIsCurrent: function (scope) {
                return workspaceReturnScopeMatchesCurrent(this, scope);
            },
            workspaceReturnWbEditMatches: function (items, selectionIds) {
                var capsule = workspaceReturnCapsule;
                if (!capsule || capsule.v !== 5) return true;
                if (!this.wbAccountKnown ||
                    capsule.wbEditTarget.accountId !== this.wbAccountId ||
                    !sameOrderedIds(selectionIds, capsule.selectionIds)) return false;
                return sameOrderedIds(
                    orderedWbProductIds(items, selectionIds),
                    capsule.wbEditTarget.productIds,
                );
            },
            workspaceReturnOzonUpdateMatches: function (items, selectionIds) {
                var capsule = workspaceReturnCapsule;
                if (!capsule || capsule.v !== 6) return true;
                var target = capsule.ozonUpdateTarget;
                var feedIds = orderedProductIds(items);
                if (!target || !sameOrderedIds(selectionIds, capsule.selectionIds) || !feedIds) return false;
                var itemsById = new Map((items || []).map(function (item) {
                    return [item.id, item];
                }));
                var selectedItems = selectionIds.map(function (id) {
                    return itemsById.get(id);
                });
                if (selectedItems.some(function (item) { return !item; })) return false;
                var result = exactOzonWorkspaceUpdateAccountChoices(
                    selectedItems, selectionIds, this.ozonAccounts,
                );
                var choice = result.choices.find(function (row) {
                    return row.account_id === target.accountId;
                });
                return !!choice && sameOrderedIds(choice.listing_ids, target.listingIds);
            },
            workspaceReturnSelectionMatches: function (items, selectionIds) {
                return this.workspaceReturnWbEditMatches(items, selectionIds) &&
                    this.workspaceReturnOzonUpdateMatches(items, selectionIds);
            },
            buildWorkspaceReturnScope: function () {
                if (this.loading || this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) return null;
                var visibleCount = Math.min(this.items.length, MAX_BULK_SELECTION);
                if (!visibleCount || !orderedProductIds(this.items.slice(0, visibleCount))) return null;
                var scope = workspaceReturnScopeSnapshot(this, visibleCount);
                return validWorkspaceReturnScope(scope) &&
                    (!this.importBatchMode || defaultWorkspaceReturnScope(scope)) ? scope : null;
            },
            wbEditSelectionIsCurrent: function (handoff) {
                if (!handoff || this._wbEditHandoff !== handoff ||
                    !handoff.abort || handoff.abort.signal.aborted ||
                    this.loading || this.loadingMore || this.selectAllLoading ||
                    this._feedRevision !== handoff.feedRevision ||
                    this.feedScopeKey() !== handoff.scopeKey ||
                    this.itemsScope !== handoff.scopeKey ||
                    !sameOrderedIds(this.selected, handoff.selectionIds) ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search ||
                    !this.wbAccountKnown || this.wbAccountId !== handoff.accountId ||
                    !workspaceReturnScopeMatchesCurrent(this, handoff.returnScope)) return false;
                return sameOrderedIds(
                    orderedWbProductIds(this.selectionItemsForIds(handoff.selectionIds), handoff.selectionIds),
                    handoff.productIds,
                );
            },
            openWbBulkEdit: function (selectionIds) {
                if (this._wbEditHandoff) this.invalidateWbEditHandoff();
                var selectionItems = this.selectionItemsForIds(selectionIds);
                var productIds = orderedWbProductIds(selectionItems, selectionIds);
                if (!this.wbConnected || !this.wbAccountKnown) {
                    this.error = 'Подключите кабинет Wildberries и обновите список перед изменением карточек.';
                    return false;
                }
                if (!this.selectionRowsVerified || !productIds || !sameOrderedIds(this.selected, selectionIds) ||
                    selectionIds.length > MAX_BULK_SELECTION ||
                    this.loading || this.loadingMore || this.selectAllLoading ||
                    this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) {
                    this.error = 'Выбор должен содержать только опубликованные карточки WB. Обновите список и отметьте товары заново.';
                    return false;
                }
                var scope = this.buildWorkspaceReturnScope();
                if (!scope) {
                    this.error = 'Не удалось сохранить условия списка. Дождитесь загрузки и повторите действие.';
                    return false;
                }
                var nonce = storeWbEditWorkspaceReturn(this, productIds, selectionIds, scope);
                var hasReturnCapsule = !!workspaceReturnHash(nonce);
                var returnTo = wbWorkspaceReturnUrl(this, hasReturnCapsule ? nonce : null);
                if (!returnTo) {
                    if (hasReturnCapsule) this.consumeWorkspaceReturn(nonce);
                    this.error = 'Не удалось подготовить безопасную ссылку возврата. Обновите страницу и повторите действие.';
                    return false;
                }
                if (!this.urls.wbSelectionResolve || !this.urls.wbBulkEdit) {
                    this.consumeWorkspaceReturn(nonce);
                    this.error = 'Не удалось открыть проверку выбора WB. Обновите страницу и повторите действие.';
                    return false;
                }

                var handoff = {
                    selectionIds: selectionIds.slice(),
                    productIds: productIds.slice(),
                    scopeKey: this.feedScopeKey(),
                    returnScope: scope,
                    feedRevision: this._feedRevision || 0,
                    accountId: this.wbAccountId,
                    returnNonce: hasReturnCapsule ? nonce : null,
                    abort: new AbortController(),
                    submitted: false,
                };
                this._wbEditHandoff = handoff;
                this.wbEditBusy = true;
                this.error = '';
                var request;
                try {
                    request = this.$api.post(this.urls.wbSelectionResolve, {
                        mode: 'workspace',
                        ids: handoff.selectionIds.slice(),
                        wb_ids: handoff.productIds.slice(),
                        return_to: returnTo,
                    }, { signal: handoff.abort.signal });
                } catch (err) {
                    request = Promise.reject(err);
                }
                var self = this;
                return Promise.resolve(request).then(function (data) {
                    if (!self.wbEditSelectionIsCurrent(handoff)) {
                        self.invalidateWbEditHandoff(handoff);
                        return false;
                    }
                    if (!validWbWorkspaceSelectionResponse(data, handoff.productIds)) {
                        self.error = 'Проверенный WB-набор не совпал с выбором. Обновите список и отметьте товары заново.';
                        return false;
                    }
                    self._wbEditHandoff = null;
                    self.wbEditBusy = false;
                    handoff.submitted = self.postWbSelectionTokenForm(data.selection_token);
                    if (!handoff.submitted) self.consumeWorkspaceReturn(handoff.returnNonce);
                    return handoff.submitted;
                }).catch(function (err) {
                    if (!self.wbEditSelectionIsCurrent(handoff)) {
                        self.invalidateWbEditHandoff(handoff);
                        return false;
                    }
                    self.error = err && err.status === 409
                        ? 'Состав выбора или связь с WB изменилась. Обновите список и отметьте карточки заново.'
                        : err && err.status === 403
                            ? 'У этого аккаунта нет доступа к выбранным карточкам Wildberries.'
                            : 'Не удалось проверить точный выбор WB. Повторите действие со свежим списком.';
                    return false;
                }).finally(function () {
                    if (self._wbEditHandoff === handoff) {
                        self._wbEditHandoff = null;
                        self.wbEditBusy = false;
                        if (handoff.returnNonce) self.consumeWorkspaceReturn(handoff.returnNonce);
                    }
                });
            },
            postWbSelectionTokenForm: function (selectionToken) {
                var csrf = document.querySelector('meta[name="csrf-token"]')?.content;
                if (!this.urls.wbBulkEdit || typeof selectionToken !== 'string' ||
                    !selectionToken || selectionToken.length > 24 * 1024) {
                    this.error = 'Не удалось открыть редактор WB с проверенным выбором. Обновите страницу и повторите действие.';
                    return false;
                }
                if (!csrf) {
                    this.error = 'Защита формы недоступна. Обновите страницу и повторите действие.';
                    return false;
                }
                var form = document.createElement('form');
                form.method = 'post';
                form.action = this.urls.wbBulkEdit;
                [
                    ['csrf_token', csrf],
                    ['selection_token', selectionToken],
                ].forEach(function (entry) {
                    var input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = entry[0];
                    input.value = entry[1];
                    form.appendChild(input);
                });
                document.body.appendChild(form);
                form.submit();
                return true;
            },
            navigateCommonContent: function (editorIds, selectionIds) {
                var scope = this.buildWorkspaceReturnScope();
                if (!scope) {
                    this.error = 'Список обновляется или условия недоступны. Дождитесь загрузки и повторите действие.';
                    return false;
                }
                var helpers = window.SellerHubCommonContent || {};
                var target;
                try {
                    target = commonContentSelectionUrl(
                        this.urls.commonContent, editorIds, helpers
                    );
                } catch (_) {
                    this.error = 'Не удалось открыть данные товара. Обновите страницу и повторите действие.';
                    return false;
                }
                var nonce = null;
                if (typeof helpers.storeWorkspaceReturn === 'function') {
                    try {
                        nonce = helpers.storeWorkspaceReturn({
                            owner: boot.workspaceOwner,
                            editorIds: editorIds,
                            selectionIds: selectionIds,
                            scope: scope,
                            batch: this.importBatchMode ? this.importBatch : null,
                        });
                        saveSelectionReviewReturn(this, nonce, selectionIds);
                    } catch (_) {
                        nonce = null;
                    }
                }
                try {
                    window.location.assign(typeof nonce === 'string' && /^[a-f0-9]{32}$/.test(nonce)
                        ? target + '#workspace-return=' + nonce
                        : target);
                    return true;
                } catch (_) {
                    if (typeof nonce === 'string' && /^[a-f0-9]{32}$/.test(nonce)) {
                        this.consumeWorkspaceReturn(nonce);
                    }
                    this.error = 'Не удалось открыть данные товара. Обновите страницу и повторите действие.';
                    return false;
                }
            },
            openCommonContentRow: function (event, item) {
                if (!event || event.button !== 0 || event.metaKey || event.ctrlKey ||
                    event.shiftKey || event.altKey || event.defaultPrevented) return;
                event.preventDefault();
                if (!item || !Number.isSafeInteger(item.id) || item.id <= 0) {
                    this.error = 'Не удалось проверить товар. Обновите список и повторите действие.';
                    return;
                }
                this.navigateCommonContent([item.id], this.selected.slice());
            },
            feedScopeKey: function () {
                if (this.importBatchMode) {
                    return JSON.stringify({ import_batch: this.importBatch });
                }
                if (this.supplierRefreshBatchMode) {
                    return JSON.stringify({ supplier_refresh_batch: this.supplierRefreshBatch });
                }
                var params = this.queryParams(1, 50);
                delete params.page;
                delete params.per_page;
                return JSON.stringify(params);
            },
            cancelFilteredSelect: function () {
                if (this._selectAllAbort) this._selectAllAbort.abort();
                this._selectAllAbort = null;
                this._selectAllSelectionSnapshot = null;
                this._selectAllRevision = (this._selectAllRevision || 0) + 1;
                this.selectAllLoading = false;
            },
            load: function () {
                var self = this;
                this.invalidateWbEditHandoff();
                this.invalidateOzonWorkspaceUpdate();
                this.cancelSupplierRefreshReview({ restoreFocus: false });
                var workspaceReturn = this.workspaceReturnPending ? workspaceReturnCapsule : null;
                if (workspaceReturn && !this.workspaceReturnScopeMatches(workspaceReturn.scope)) {
                    this.workspaceReturnChanged();
                    workspaceReturn = null;
                }
                var restoreSelectionReview = !!workspaceReturn &&
                    this.workspaceSelectionReviewReturnMatches(workspaceReturn);
                this.cancelSelectionReviewRequest();
                if (!restoreSelectionReview) {
                    this.cancelSelectionReview({ clearRows: true });
                } else {
                    this.selectionReviewActive = false;
                    this.selectionReviewRows = [];
                    this.selectionReviewIds = [];
                }
                if (this._abort) this._abort.abort();
                if (this._loadMoreAbort) this._loadMoreAbort.abort();
                this._loadMoreAbort = null;
                this.loadingMore = false;
                this.cancelFilteredSelect();
                if (this.importBatchMode || this.supplierRefreshBatchMode) this.selected = [];
                if (this.importBatchMode) this.importBatchSelectionVerified = false;
                var revision = (this._feedRevision || 0) + 1;
                this._feedRevision = revision;
                var scope = this.feedScopeKey();
                var restorePerPage = workspaceReturn && !this.importBatchMode &&
                    !this.supplierRefreshBatchMode
                    ? Math.ceil(workspaceReturn.scope.visibleCount / 50) * 50
                    : undefined;
                var abort = new AbortController();
                this._abort = abort;
                this.loading = true;
                this.error = '';
                if (this.importBatchMode || this.supplierRefreshBatchMode) {
                    // Never leave rows from a previous scope actionable while
                    // the exact imported batch is being re-read.
                    this.items = [];
                    this.itemsScope = '';
                    this.pagination = {};
                    if (this.importBatchInvalid || this.supplierRefreshBatchInvalid) {
                        this.supplierRefreshSelectPending = false;
                        this.error = this.supplierRefreshBatchMode
                            ? 'Выбранный список недоступен. Вернитесь к классическому списку.'
                            : this.supplierRefreshResultBatchMode
                                ? 'Список обновлённых товаров недоступен. Итог обновления остаётся на этой странице.'
                                : 'Эта подборка недоступна. Вернитесь через «Все мои товары».';
                        this.loading = false;
                        if (this._abort === abort) this._abort = null;
                        return;
                    }
                }
                this.$api.get(this.urls.feed, this.queryParams(1, restorePerPage), { signal: abort.signal })
                    .then(function (data) {
                        if (abort.signal.aborted || self._feedRevision !== revision) return;
                        if (workspaceReturn) {
                            if (!self.workspaceReturnScopeMatches(workspaceReturn.scope)) {
                                self.workspaceReturnChanged();
                                return;
                            }
                            if (self.importBatchMode) {
                                var returnBatchKind = self.supplierRefreshResultBatchMode
                                    ? 'supplier_refresh_result' : 'import';
                                if (!exactImportBatchPage(data, returnBatchKind)) {
                                    self.items = [];
                                    self.itemsScope = '';
                                    self.pagination = {};
                                    self.workspaceReturnChanged();
                                    self.error = self.supplierRefreshResultBatchMode
                                        ? 'Не удалось подтвердить обновлённый список после возврата. Итог обновления сохранён; отметьте товары заново.'
                                        : 'Не удалось подтвердить состав этой подборки. Вернитесь через «Все мои товары». Обновите список и отметьте товары снова.';
                                    return;
                                }
                                var batchIds = orderedProductIds(data.items);
                                self.items = data.items;
                                self.pagination = data.pagination;
                                self.itemsScope = scope;
                                if (self.importBatchKind === 'import' &&
                                    !self.supplierRefreshResultBatchMode) {
                                    self.importBatchSelectionVerified = true;
                                }
                                if (batchIds && workspaceReturn.selectionIds.every(function (id) {
                                    return batchIds.indexOf(id) !== -1;
                                }) && self.workspaceReturnSelectionMatches(
                                    data.items, workspaceReturn.selectionIds,
                                )) {
                                    self.selected = workspaceReturn.selectionIds.slice();
                                } else {
                                    self.workspaceReturnChanged();
                                }
                                return;
                            }

                            var returnedPage = exactWorkspaceFeedPage(data, restorePerPage);
                            if (!returnedPage) {
                                self.items = [];
                                self.itemsScope = '';
                                self.pagination = {};
                                self.workspaceReturnChanged();
                                self.error = 'Не удалось проверить список после возврата. Обновите его и отметьте товары снова.';
                                return;
                            }
                            self.items = data.items;
                            self.itemsScope = scope;
                            self.pagination = Object.assign({}, returnedPage.pagination, {
                                page: Math.max(1, Math.ceil(data.items.length / 50)),
                                per_page: 50,
                                pages: Math.ceil(returnedPage.pagination.total / 50),
                                has_next: data.items.length < returnedPage.pagination.total,
                            });
                            if (restoreSelectionReview) {
                                return self.restoreWorkspaceSelectionReview(
                                    workspaceReturn, scope, revision,
                                );
                            }
                            if (workspaceReturn.selectionIds.every(function (id) {
                                return returnedPage.ids.indexOf(id) !== -1;
                            }) && self.workspaceReturnSelectionMatches(
                                data.items, workspaceReturn.selectionIds,
                            )) {
                                self.selected = workspaceReturn.selectionIds.slice();
                            } else {
                                self.workspaceReturnChanged();
                            }
                            return;
                        }
                        if (self.importBatchMode) {
                            var feedBatchKind = self.supplierRefreshResultBatchMode
                                ? 'supplier_refresh_result' : 'import';
                            if (!exactImportBatchPage(data, feedBatchKind)) {
                                self.items = [];
                                self.itemsScope = '';
                                self.pagination = {};
                                self.supplierRefreshSelectPending = false;
                                self.importBatchSelectionPending = false;
                                var serverError = data && data.success === false &&
                                    typeof data.error === 'string' ? data.error.trim() : '';
                                self.error = self.supplierRefreshResultBatchMode
                                    ? 'Не удалось подтвердить полный обновлённый список. Итог обновления сохранён; обновите страницу и повторите выбор.'
                                    : serverError
                                    ? serverError.slice(0, 300) + ' Вернитесь через «Все мои товары».'
                                    : 'Не удалось подтвердить состав этой подборки. Вернитесь через «Все мои товары».';
                                return;
                            }
                            var verifiedBatchIds = orderedProductIds(data.items);
                            if (self.importBatchSelectionPending &&
                                (self.importBatchKind !== 'import' ||
                                    !Number.isSafeInteger(self.importBatchExpectedCount) ||
                                    data.import_batch_count !== self.importBatchExpectedCount ||
                                    verifiedBatchIds.length !== self.importBatchExpectedCount)) {
                                self.items = [];
                                self.itemsScope = '';
                                self.pagination = {};
                                self.importBatchSelectionPending = false;
                                self.error = 'Список не совпал с подтверждённым итогом импорта. Товары не выбраны; обновите список и отметьте их заново.';
                                return;
                            }
                            if (self.supplierRefreshSelectPending) {
                                var result = self.supplierRefreshResult;
                                var resultUpdatedIds = result && Array.isArray(result.entries)
                                    ? result.entries.filter(function (entry) {
                                        return entry && entry.status === 'updated';
                                    }).map(function (entry) { return entry.productId; }) : [];
                                if (!result || resultUpdatedIds.length !== result.updated ||
                                    !sameOrderedIds(verifiedBatchIds, resultUpdatedIds)) {
                                    self.items = [];
                                    self.itemsScope = '';
                                    self.pagination = {};
                                    self.supplierRefreshSelectPending = false;
                                    self.error = 'Список не совпал с подтверждёнными обновлёнными карточками. Итог обновления сохранён; товары не выбраны.';
                                    return;
                                }
                            }
                            self.items = data.items;
                            self.pagination = data.pagination;
                            self.itemsScope = scope;
                            if (self.importBatchKind === 'import' &&
                                !self.supplierRefreshResultBatchMode) {
                                self.importBatchSelectionVerified = true;
                            }
                            if (self.importBatchSelectionPending) {
                                self.selected = verifiedBatchIds.slice();
                                self.importBatchSelectionPending = false;
                                self.importBatchExpectedCount = 0;
                            }
                            if (self.supplierRefreshSelectPending) {
                                self.selected = verifiedBatchIds.slice();
                            }
                            self.supplierRefreshSelectPending = false;
                            if (self.importBatchKind === 'import' && !self.supplierRefreshResultBatchMode) {
                                self.tryResumeWbCreateAfterFeed();
                            }
                            return;
                        }
                        if (self.supplierRefreshBatchMode) {
                            if (!exactSupplierRefreshBatchPage(data)) {
                                self.items = [];
                                self.itemsScope = '';
                                self.pagination = {};
                                var handoffError = data && data.success === false &&
                                    typeof data.error === 'string' ? data.error.trim() : '';
                                self.error = handoffError
                                    ? handoffError.slice(0, 300) + ' Вернитесь к классическому списку.'
                                    : 'Не удалось подтвердить весь выбранный список. Вернитесь к классическому списку.';
                                return;
                            }
                            self.items = data.items;
                            self.pagination = data.pagination;
                            self.itemsScope = scope;
                            self.selected = orderedProductIds(data.items).slice();
                            return;
                        }
                        self.items = data.items || [];
                        self.pagination = data.pagination || {};
                        self.itemsScope = scope;
                    }).catch(function (err) {
                        if ((err && err.name === 'AbortError') || abort.signal.aborted ||
                            self._feedRevision !== revision) return;
                        if (workspaceReturn) {
                            self.workspaceReturnChanged();
                            self.error = self.supplierRefreshResultBatchMode
                                ? 'Не удалось загрузить обновлённый список после возврата. Итог обновления сохранён; повторите выбор.'
                                : self.importBatchMode
                                    ? 'Не удалось загрузить подборку после возврата. Обновите список и отметьте товары снова.'
                                    : 'Не удалось проверить список после возврата. Обновите его и отметьте товары снова.';
                            return;
                        }
                        if (self.importBatchMode || self.supplierRefreshBatchMode) {
                            self.supplierRefreshSelectPending = false;
                            if (err && [400, 404, 410].includes(err.status)) {
                                self.importBatchSelectionPending = false;
                                self.importBatchExpectedCount = 0;
                            }
                            self.items = [];
                            self.itemsScope = '';
                            self.pagination = {};
                            if (self.supplierRefreshBatchMode) {
                                self.error = err && err.status === 410
                                    ? 'Срок действия выбранного списка истёк. Вернитесь к классическому списку и выберите товары снова.'
                                    : err && (err.status === 400 || err.status === 404)
                                        ? 'Выбранный список недоступен. Вернитесь к классическому списку и выберите товары снова.'
                                        : 'Не удалось загрузить выбранный список. Повторите загрузку или вернитесь к классическому списку.';
                                return;
                            }
                            if (self.supplierRefreshResultBatchMode) {
                                self.error = err && err.status === 410
                                    ? 'Срок действия списка обновлённых товаров истёк. Итог обновления сохранён.'
                                    : 'Не удалось загрузить список обновлённых товаров. Итог обновления сохранён; повторите выбор.';
                                return;
                            }
                            if (err && err.status === 410) {
                                var expiryMessage = typeof err.message === 'string'
                                    ? err.message.trim().slice(0, 300) : '';
                                if (expiryMessage && !/[.!?]$/.test(expiryMessage)) expiryMessage += '.';
                                self.error = (expiryMessage || 'Срок действия подборки импорта истёк.') +
                                    ' Вернитесь через «Все мои товары».';
                            } else if (err && (err.status === 400 || err.status === 404)) {
                                self.error = 'Подборка импорта недоступна. Вернитесь через «Все мои товары».';
                            } else {
                                self.error = 'Не удалось загрузить подборку импорта. Повторите загрузку; если ошибка повторится, вернитесь через «Все мои товары».';
                            }
                            return;
                        }
                        self.error = err.message || 'Не удалось загрузить товары';
                    }).finally(function () {
                        if (self._abort === abort) {
                            self._abort = null;
                            self.loading = false;
                        }
                        if (workspaceReturn && self.workspaceReturnPending &&
                            self._feedRevision === revision) {
                            self.consumeWorkspaceReturn(workspaceReturn.nonce);
                        }
                    });
            },
            loadMore: function () {
                if (this.importBatchMode || this.supplierRefreshBatchMode || this.selectionReviewActive) return;
                if (this.loading || this.loadingMore || !this.pagination.has_next ||
                    this.itemsScope !== this.feedScopeKey()) return;
                var self = this;
                var revision = this._feedRevision || 0;
                var scope = this.feedScopeKey();
                var currentPage = this.pagination.page;
                var total = this.pagination.total;
                var perPage = this.pagination.per_page;
                if (!Number.isSafeInteger(currentPage) || currentPage < 1 ||
                    !Number.isSafeInteger(total) || total < 0 ||
                    !Number.isSafeInteger(perPage) || perPage < 1) {
                    this.pagination = Object.assign({}, this.pagination, { has_next: false });
                    this.error = 'Список изменился. Обновите его перед продолжением.';
                    return;
                }
                var nextPage = currentPage + 1;
                var abort = new AbortController();
                this._loadMoreAbort = abort;
                this.loadingMore = true;
                this.$api.get(this.urls.feed, this.queryParams(nextPage), { signal: abort.signal })
                    .then(function (data) {
                        if (abort.signal.aborted || self._feedRevision !== revision ||
                            self.feedScopeKey() !== scope) return;
                        var pageIds = data && orderedProductIds(data.items);
                        var existingIds = orderedProductIds(self.items);
                        var expectedCount = Math.min(perPage, Math.max(0, total - currentPage * perPage));
                        var consistent = self.pagination.page === currentPage &&
                            self.pagination.total === total && data && data.pagination &&
                            data.pagination.page === nextPage &&
                            data.pagination.per_page === perPage && data.pagination.total === total &&
                            typeof data.pagination.has_next === 'boolean' &&
                            Array.isArray(data.items) && data.items.length === expectedCount &&
                            pageIds && existingIds &&
                            !pageIds.some(function (id) { return existingIds.indexOf(id) !== -1; });
                        if (!consistent) {
                            self.pagination = Object.assign({}, self.pagination, { has_next: false });
                            self.error = 'Список изменился. Обновите его перед продолжением.';
                            return;
                        }
                        var seen = {};
                        self.items.forEach(function (item) { seen[item.id] = true; });
                        (data.items || []).forEach(function (item) {
                            if (!seen[item.id]) self.items.push(item);
                        });
                        self.pagination = data.pagination || self.pagination;
                    }).catch(function (err) {
                        if ((err && err.name === 'AbortError') || abort.signal.aborted ||
                            self._feedRevision !== revision || self.feedScopeKey() !== scope) return;
                        self.error = err.message || 'Не удалось загрузить ещё';
                    }).finally(function () {
                        if (self._loadMoreAbort === abort) {
                            self._loadMoreAbort = null;
                            self.loadingMore = false;
                        }
                    });
            },
            selectAllFiltered: function () {
                if (this.importBatchMode || this.supplierRefreshBatchMode || this.selectionReviewActive) return;
                if (!this.canSelectAllFiltered || this.selectAllLoading) return;
                var self = this;
                var total = this.pagination.total;
                var scope = this.feedScopeKey();
                if (this.itemsScope !== scope) return;
                var shownIds = orderedProductIds(this.items);
                var selectionSnapshot = this.selected.slice();
                if (!shownIds || new Set(selectionSnapshot).size !== selectionSnapshot.length) {
                    this.error = 'Не удалось проверить текущий список. Обновите его и повторите выбор.';
                    return;
                }
                var params = this.queryParams(1, MAX_BULK_SELECTION);
                var revision = (this._selectAllRevision || 0) + 1;
                this._selectAllRevision = revision;
                var abort = new AbortController();
                this._selectAllAbort = abort;
                this._selectAllSelectionSnapshot = selectionSnapshot;
                this.selectAllLoading = true;
                this.error = '';
                this.$api.get(this.urls.feed, params, { signal: abort.signal })
                    .then(function (data) {
                        if (abort.signal.aborted || self._selectAllAbort !== abort ||
                            self._selectAllRevision !== revision || self.feedScopeKey() !== scope ||
                            (self.searchInput || '').trim().slice(0, 200) !== self.filters.search ||
                            !sameOrderedIds(self.selected, selectionSnapshot)) return;

                        var fullIds = data && orderedProductIds(data.items);
                        var responsePage = data && data.pagination;
                        var currentShownIds = orderedProductIds(self.items);
                        var valid = data && data.success === true && Array.isArray(data.items) &&
                            responsePage && responsePage.page === 1 &&
                            responsePage.per_page === MAX_BULK_SELECTION &&
                            responsePage.total === total && responsePage.has_next === false &&
                            data.items.length === total && fullIds && fullIds.length === total &&
                            sameOrderedIds(shownIds, fullIds.slice(0, shownIds.length)) &&
                            currentShownIds &&
                            sameOrderedIds(currentShownIds, fullIds.slice(0, currentShownIds.length));
                        if (!valid) {
                            self.error = 'Список изменился или не загрузился целиком. Обновите его и повторите выбор.';
                            return;
                        }

                        var union = selectionSnapshot.slice();
                        fullIds.forEach(function (id) {
                            if (union.indexOf(id) === -1) union.push(id);
                        });
                        if (union.length > MAX_BULK_SELECTION) {
                            self.error = 'За один раз можно выбрать не больше 200 товаров. Снимите часть отметок и повторите.';
                            return;
                        }
                        self._selectAllAbort = null;
                        self._selectAllSelectionSnapshot = null;
                        self.selectAllLoading = false;
                        if (self._loadMoreAbort) self._loadMoreAbort.abort();
                        self._loadMoreAbort = null;
                        self.loadingMore = false;
                        self._feedRevision = (self._feedRevision || 0) + 1;
                        self.items = data.items;
                        self.pagination = responsePage;
                        self.itemsScope = scope;
                        self.selected = union;
                    }).catch(function (err) {
                        if ((err && err.name === 'AbortError') || abort.signal.aborted ||
                            self._selectAllAbort !== abort || self._selectAllRevision !== revision ||
                            self.feedScopeKey() !== scope) return;
                        self.error = err.message || 'Не удалось выбрать весь результат. Повторите попытку.';
                    }).finally(function () {
                        if (self._selectAllAbort === abort) {
                            self._selectAllAbort = null;
                            self._selectAllSelectionSnapshot = null;
                            self.selectAllLoading = false;
                        }
                    });
            },
            loadFacets: function () {
                if (this.importBatchMode || this.supplierRefreshBatchMode) return;
                var self = this;
                this.$api.get(this.urls.facets).then(function (data) {
                    self.facets = data.statuses || {};
                }).catch(function () { /* счётчики некритичны */ });
            },
            loadCatalogCategories: function () {
                if (this._mpUnmounted || this.importBatchMode || this.supplierRefreshBatchMode ||
                    this.catalogCategoriesLoading) return Promise.resolve(false);
                if (this.catalogCategoriesPage > 0 && !this.catalogCategoriesHasNext &&
                    !this.catalogCategoriesError) return Promise.resolve(false);
                var requestedPage = this.catalogCategoriesPage > 0
                    ? this.catalogCategoriesPage + 1 : 1;
                if (requestedPage > 1 && !this.catalogCategoriesHasNext &&
                    !this.catalogCategoriesError) return Promise.resolve(false);
                if (typeof this.urls.catalogCategories !== 'string' ||
                    this.urls.catalogCategories.charAt(0) !== '/') {
                    this.catalogCategoriesError = 'Список категорий не загружен. Повторите загрузку.';
                    return Promise.resolve(false);
                }
                var requestId = (this._catalogCategoriesRequestId || 0) + 1;
                this._catalogCategoriesRequestId = requestId;
                var abort = new AbortController();
                this._catalogCategoriesAbort = abort;
                this.catalogCategoriesLoading = true;
                this.catalogCategoriesError = '';
                var self = this;
                return this.$api.get(this.urls.catalogCategories, { page: requestedPage }, { signal: abort.signal })
                    .then(function (data) {
                        if (self._mpUnmounted || abort.signal.aborted ||
                            self._catalogCategoriesAbort !== abort ||
                            self._catalogCategoriesRequestId !== requestId) return false;
                        if (!validCatalogCategoryPage(data, requestedPage)) {
                            throw new Error('invalid_categories_response');
                        }
                        var knownKeys = new Set(self.catalogCategoryOptions.map(function (option) {
                            return option.key;
                        }));
                        self.catalogCategoryOptions = self.catalogCategoryOptions.concat(
                            data.options.filter(function (option) {
                                if (knownKeys.has(option.key)) return false;
                                knownKeys.add(option.key);
                                return true;
                            }).map(function (option) {
                                return { key: option.key, label: option.label };
                            }),
                        );
                        self.catalogCategoriesPage = requestedPage;
                        self.catalogCategoriesHasNext = data.has_next;
                        self.catalogCategoriesHasUncategorized =
                            self.catalogCategoriesHasUncategorized || data.has_uncategorized;
                        return true;
                    }).catch(function (error) {
                        if (self._mpUnmounted || abort.signal.aborted ||
                            self._catalogCategoriesAbort !== abort ||
                            self._catalogCategoriesRequestId !== requestId ||
                            (error && error.name === 'AbortError')) return false;
                        self.catalogCategoriesError = 'Список категорий не загружен. Повторите загрузку.';
                        return false;
                    }).finally(function () {
                        if (self._catalogCategoriesAbort === abort) {
                            self._catalogCategoriesAbort = null;
                            self.catalogCategoriesLoading = false;
                        }
                    });
            },
            handleCatalogCategoryChange: function (event) {
                var select = event && event.currentTarget;
                if (!select) return false;
                var value = select.value;
                if (value === CATALOG_CATEGORY_MORE_OPTION) {
                    select.value = this.filters.catalog_category || '';
                    this.loadCatalogCategories();
                    return false;
                }
                if (value !== '' && (!validCatalogCategoryToken(value) ||
                    !this.catalogCategorySelectOptions.some(function (option) {
                        return option.key === value;
                    }))) {
                    select.value = this.filters.catalog_category || '';
                    return false;
                }
                this.filters.catalog_category = value;
                return true;
            },
            setTab: function (key) {
                if (this.importBatchMode || this.supplierRefreshBatchMode) return;
                if (['', 'updates'].indexOf(key) === -1 || this.activeTab === key) return;
                this.activeTab = key;
                this.load();
            },
            setMarketplace: function (marketplace) {
                if (this.importBatchMode || this.supplierRefreshBatchMode ||
                    ['', 'wb', 'ozon'].indexOf(marketplace) === -1 ||
                    (marketplace === 'ozon' && !this.ozonEnabled)) return false;
                this.filters.marketplace = marketplace;
                if (marketplace === 'ozon' && this.activeTab !== 'updates' && this.activeTab) {
                    this.activeTab = '';
                }
                return true;
            },
            setWbStatus: function (status) {
                if (this.importBatchMode || this.supplierRefreshBatchMode ||
                    this.filters.marketplace === 'ozon' ||
                    !['', 'pending', 'validated', 'imported', 'imported_pending_sync', 'failed'].includes(status)) {
                    return false;
                }
                if (status) this.activeTab = status;
                else if (this.activeTab !== 'updates') this.activeTab = '';
                this.load();
                return true;
            },
            toggleSupplierUpdates: function () {
                if (this.importBatchMode || this.supplierRefreshBatchMode ||
                    this.selected.length || this.workspaceReturnPending) return false;
                this.setTab(this.activeTab === 'updates' ? '' : 'updates');
                return true;
            },
            activateSupplierUpdatesEntry: function (event) {
                if (this.supplierUpdatesEntryDisabled) return false;
                if (!this.selected.length) return this.toggleSupplierUpdates();
                var action = this.supplierRefreshSelectionAction;
                if (!action || action.disabled) return false;
                this.runBulkIntentAction(action, event);
                return true;
            },
            resetFilters: function () {
                if (this.importBatchMode || this.supplierRefreshBatchMode) return;
                this.activeTab = '';
                this.searchInput = '';
                this.filters = {
                    search: '', supplier: '', has_photos: '', stock: '', sort: '',
                    marketplace: '', marketplace_state: '', catalog_category: '',
                };
            },
            toggleAll: function () {
                if (this.selectionReviewActive || this.loading || this.loadingMore ||
                    this.itemsScope !== this.feedScopeKey()) return;
                var shownIds = orderedProductIds(this.items);
                if (!shownIds) {
                    this.error = 'Не удалось проверить показанные товары. Обновите список и повторите выбор.';
                    return;
                }
                if (this.allSelected) {
                    var shown = new Set(shownIds);
                    this.selected = this.selected.filter(function (id) { return !shown.has(id); });
                } else {
                    var next = this.selected.slice();
                    shownIds.forEach(function (id) {
                        if (next.indexOf(id) === -1) next.push(id);
                    });
                    if (next.length > MAX_BULK_SELECTION) {
                        this.error = 'За один раз можно выбрать не больше 200 товаров. Снимите часть отметок и повторите.';
                        return;
                    }
                    this.selected = next;
                }
                this.markWbCreateSelectionIntent();
            },
            resolveOzonPriceAccountChoices: function (items, selectionIds) {
                return exactOzonPriceAccountChoices(items, selectionIds, this.ozonAccounts);
            },
            resolveOzonWorkspaceUpdateAccountChoices: function (items, selectionIds) {
                return exactOzonWorkspaceUpdateAccountChoices(
                    items, selectionIds, this.ozonAccounts,
                );
            },
            setOzonPriceHandoffError: function (message) {
                this.ozonPriceError = message;
                this.$nextTick(function () {
                    this.$refs.ozonPriceError?.focus();
                });
            },
            openOzonWorkspaceUpdate: function (items, selectionIds) {
                this.error = '';
                this.ozonWorkspaceUpdateDialogError = '';
                if (this.ozonWorkspaceUpdateBusy) return false;
                if (!this.selectionRowsVerified || !sameOrderedIds(this.selected, selectionIds) ||
                    selectionIds.length > MAX_BULK_SELECTION) {
                    this.error = 'Выбор изменился или превышает 200 товаров. Отметьте товары заново.';
                    return false;
                }
                if (this.loading || this.loadingMore || this.selectAllLoading ||
                    this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) {
                    this.error = 'Список обновляется. Дождитесь загрузки и отметьте товары заново.';
                    return false;
                }
                var result = this.resolveOzonWorkspaceUpdateAccountChoices(items, selectionIds);
                if (!result.choices.length) {
                    this.error = result.reason === 'no_accounts'
                        ? 'Подключите активный магазин Ozon, затем повторите действие.'
                        : result.reason === 'accounts'
                            ? 'Не удалось проверить магазины Ozon. Обновите страницу и повторите действие.'
                            : result.reason === 'selection'
                                ? 'Список изменился или загружен не полностью. Обновите его и отметьте товары снова.'
                                : 'Не найден один доступный магазин Ozon с точной связанной карточкой для каждого товара. Выбор не сокращён.';
                    return false;
                }
                var returnScope = this.buildWorkspaceReturnScope();
                if (!returnScope) {
                    this.error = 'Не удалось сохранить условия списка. Дождитесь загрузки и повторите действие.';
                    return false;
                }
                var review = {
                    sourceIds: selectionIds.slice(),
                    choices: result.choices.map(function (choice) {
                        return {
                            account_id: choice.account_id,
                            account_label: choice.account_label,
                            listing_ids: choice.listing_ids.slice(),
                        };
                    }),
                    scopeKey: this.feedScopeKey(),
                    feedRevision: this._feedRevision || 0,
                    returnScope: returnScope,
                };
                this.ozonWorkspaceUpdateReview = review;
                this.ozonAccountChooserKind = 'workspace_update';
                if (review.choices.length === 1) {
                    return this.submitOzonWorkspaceUpdate(review.choices[0].account_id, review);
                }
                this._ozonPriceTrigger = document.activeElement;
                this.ozonPriceAccountId = '';
                this.$nextTick(function () {
                    this.$refs.ozonPriceAccountDialog.showModal();
                    this.$refs.ozonPriceAccount.focus();
                });
                return true;
            },
            cancelOzonWorkspaceUpdateReview: function (options) {
                options = options || {};
                var dialog = this.$refs.ozonPriceAccountDialog;
                var wasUpdateChooser = this.ozonAccountChooserKind === 'workspace_update';
                if (wasUpdateChooser && dialog && dialog.open) dialog.close();
                this.ozonWorkspaceUpdateReview = null;
                this.ozonWorkspaceUpdateDialogError = '';
                if (wasUpdateChooser) {
                    this.ozonAccountChooserKind = '';
                    this.ozonPriceAccountId = '';
                    if (options.restoreFocus !== false) this._ozonPriceTrigger?.focus();
                }
            },
            ozonWorkspaceUpdateReviewIsCurrent: function (review, accountId, expectedListingIds) {
                if (!review || (this._feedRevision || 0) !== review.feedRevision ||
                    this.loading || this.loadingMore || this.selectAllLoading ||
                    this.feedScopeKey() !== review.scopeKey || this.itemsScope !== review.scopeKey ||
                    !sameOrderedIds(this.selected, review.sourceIds) ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search ||
                    !workspaceReturnScopeMatchesCurrent(this, review.returnScope)) {
                    return false;
                }
                var result = this.resolveOzonWorkspaceUpdateAccountChoices(
                    this.selectedItems, review.sourceIds,
                );
                var freshChoice = result.choices.find(function (choice) {
                    return choice.account_id === accountId;
                });
                var reviewedChoice = review.choices.find(function (choice) {
                    return choice.account_id === accountId;
                });
                return !!freshChoice && !!reviewedChoice &&
                    sameOrderedIds(freshChoice.listing_ids, reviewedChoice.listing_ids) &&
                    (!expectedListingIds || sameOrderedIds(freshChoice.listing_ids, expectedListingIds));
            },
            submitOzonWorkspaceUpdate: function (accountId, review) {
                review = review || this.ozonWorkspaceUpdateReview;
                if (!this.ozonWorkspaceUpdateReviewIsCurrent(review, accountId)) {
                    this.cancelOzonWorkspaceUpdateReview({ restoreFocus: false });
                    this.error = 'Выбор или связанные карточки Ozon изменились после проверки. Обновите список и повторите действие.';
                    return false;
                }
                var choice = review.choices.find(function (candidate) {
                    return candidate.account_id === accountId;
                });
                if (!choice || this.ozonWorkspaceUpdateBusy) return false;
                var sourceIds = review.sourceIds.slice();
                var listingIds = choice.listing_ids.slice();
                var returnNonce = storeOzonWorkspaceUpdateReturn(
                    this, accountId, listingIds, sourceIds, review.returnScope,
                );
                var returnTo = wbWorkspaceReturnUrl(this, returnNonce);
                if (!returnTo || !this.urls.ozonWorkspaceUpdateHandoff) {
                    if (returnNonce) this.consumeWorkspaceReturn(returnNonce);
                    this.cancelOzonWorkspaceUpdateReview({ restoreFocus: false });
                    this.error = 'Не удалось подготовить безопасный переход. Обновите страницу и повторите действие.';
                    return false;
                }

                this.cancelOzonWorkspaceUpdateReview({ restoreFocus: false });
                var handoff = {
                    selectionIds: sourceIds,
                    listingIds: listingIds,
                    accountId: accountId,
                    scopeKey: review.scopeKey,
                    returnScope: review.returnScope,
                    feedRevision: review.feedRevision,
                    returnNonce: returnNonce,
                    abort: new AbortController(),
                };
                this._ozonWorkspaceUpdateHandoff = handoff;
                this.ozonWorkspaceUpdateBusy = true;
                this.error = '';
                var request;
                try {
                    request = this.$api.post(this.urls.ozonWorkspaceUpdateHandoff, {
                        account_id: accountId,
                        source_ids: sourceIds.slice(),
                        listing_ids: listingIds.slice(),
                        return_to: returnTo,
                    }, { signal: handoff.abort.signal });
                } catch (err) {
                    request = Promise.reject(err);
                }
                var self = this;
                return Promise.resolve(request).then(function (data) {
                    if (!self.ozonWorkspaceUpdateReviewIsCurrent(
                        review, handoff.accountId, handoff.listingIds,
                    ) || self._ozonWorkspaceUpdateHandoff !== handoff ||
                        handoff.abort.signal.aborted) {
                        self.invalidateOzonWorkspaceUpdate(handoff);
                        return false;
                    }
                    var safeUrl = data && data.success === true
                        ? validOzonWorkspaceUpdateHandoffUrl(data.url, handoff.accountId)
                        : '';
                    if (!safeUrl) {
                        self.error = data && data.code === 'selection_changed'
                            ? 'Связь с карточкой Ozon изменилась. Обновите список и отметьте товары заново.'
                            : 'Не удалось проверить точный выбор для редактора Ozon. Повторите действие со свежим списком.';
                        return false;
                    }
                    self._ozonWorkspaceUpdateHandoff = null;
                    self.ozonWorkspaceUpdateBusy = false;
                    window.location.assign(safeUrl);
                    return true;
                }).catch(function (err) {
                    if (!self.ozonWorkspaceUpdateReviewIsCurrent(
                        review, handoff.accountId, handoff.listingIds,
                    ) || self._ozonWorkspaceUpdateHandoff !== handoff ||
                        handoff.abort.signal.aborted) {
                        self.invalidateOzonWorkspaceUpdate(handoff);
                        return false;
                    }
                    self.error = err && err.status === 409
                        ? 'Состав выбора или связь с Ozon изменилась. Обновите список и отметьте товары заново.'
                        : err && err.status === 403
                            ? 'У этого аккаунта нет доступа к выбранным карточкам Ozon.'
                            : 'Не удалось проверить точный выбор Ozon. Повторите действие со свежим списком.';
                    return false;
                }).finally(function () {
                    if (self._ozonWorkspaceUpdateHandoff === handoff) {
                        self._ozonWorkspaceUpdateHandoff = null;
                        self.ozonWorkspaceUpdateBusy = false;
                        if (handoff.returnNonce) self.consumeWorkspaceReturn(handoff.returnNonce);
                    }
                });
            },
            openOzonPriceHandoff: function (items, selectionIds, options) {
                options = options || {};
                var source = options.source === 'row' ? 'row' : 'bulk';
                this.ozonPriceError = '';
                this.ozonPriceDialogError = '';
                this.ozonAccountChooserKind = 'price';
                if (!Array.isArray(selectionIds) || !selectionIds.length ||
                    (source === 'bulk' && !sameOrderedIds(this.selected, selectionIds)) ||
                    (source === 'row' && selectionIds.length !== 1) ||
                    this.loading || this.loadingMore || this.selectAllLoading ||
                    this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) {
                    this.setOzonPriceHandoffError(
                        'Список или выбор изменился. Обновите список и повторите действие.'
                    );
                    return;
                }
                var sourceItems = this.priceWorkspaceItemsBySelection(selectionIds);
                if (!sourceItems) {
                    this.setOzonPriceHandoffError(
                        'Не удалось проверить точный список товаров. Обновите страницу и повторите действие.'
                    );
                    return;
                }
                var result = this.resolveOzonPriceAccountChoices(sourceItems, selectionIds);
                if (!result.choices.length) {
                    var message = result.reason === 'limit'
                        ? 'За одно сравнение Ozon можно выбрать не больше 100 товаров.'
                        : result.reason === 'no_accounts'
                            ? 'Нет активного магазина Ozon. Подключите магазин или откройте «Ценовые заявки Ozon».'
                            : result.reason === 'selection'
                                ? 'Список товаров изменился или неполон. Обновите его и отметьте товары заново.'
                                : result.reason === 'accounts'
                                    ? 'Не удалось проверить список магазинов Ozon. Обновите страницу и повторите.'
                                    : 'Не найден один магазин Ozon с точной связанной, доступной и неархивной карточкой для каждого выбранного товара. Выбор не сокращён. Проверьте карточки или откройте «Ценовые заявки Ozon».';
                    this.setOzonPriceHandoffError(message);
                    return;
                }
                var review = {
                    imported_product_ids: selectionIds.slice(),
                    source: source,
                    scopeKey: this.feedScopeKey(),
                    feedRevision: this._feedRevision,
                    returnScope: source === 'bulk' ? this.buildWorkspaceReturnScope() : null,
                    returnFocus: options.returnFocus || document.activeElement,
                    choices: result.choices.map(function (choice) {
                        return {
                            account_id: choice.account_id,
                            account_label: choice.account_label,
                            listing_ids: choice.listing_ids.slice(),
                        };
                    }),
                };
                this.ozonPriceHandoffReview = review;
                if (review.choices.length === 1) {
                    this.navigateOzonPriceHandoff(review.choices[0].account_id, review);
                    return;
                }
                this._ozonPriceTrigger = review.returnFocus;
                this.ozonPriceAccountId = '';
                this.$nextTick(function () {
                    this.$refs.ozonPriceAccountDialog.showModal();
                    this.$refs.ozonPriceAccount.focus();
                });
            },
            closeOzonPriceHandoff: function (options) {
                options = options || {};
                var dialog = this.$refs.ozonPriceAccountDialog;
                if (dialog && dialog.open) dialog.close();
                this.ozonPriceHandoffReview = null;
                this.ozonWorkspaceUpdateReview = null;
                this.ozonPriceAccountId = '';
                this.ozonPriceDialogError = '';
                this.ozonWorkspaceUpdateDialogError = '';
                this.ozonAccountChooserKind = '';
                if (options.restoreFocus !== false) this._ozonPriceTrigger?.focus();
            },
            continueOzonPriceHandoff: function () {
                if (this.ozonAccountChooserKind === 'workspace_update') {
                    var updateReview = this.ozonWorkspaceUpdateReview;
                    var updateAccountId = Number(this.ozonPriceAccountId);
                    if (!updateReview || !Number.isSafeInteger(updateAccountId) || updateAccountId <= 0 ||
                        !updateReview.choices.some(function (choice) {
                            return choice.account_id === updateAccountId;
                        })) {
                        this.ozonWorkspaceUpdateDialogError = 'Выберите магазин из списка.';
                        this.$nextTick(function () {
                            this.$refs.ozonPriceAccount.focus();
                        });
                        return;
                    }
                    return this.submitOzonWorkspaceUpdate(updateAccountId, updateReview);
                }
                var review = this.ozonPriceHandoffReview;
                var accountId = Number(this.ozonPriceAccountId);
                if (!review || !Number.isSafeInteger(accountId) || accountId <= 0 ||
                    !review.choices.some(function (choice) {
                        return choice.account_id === accountId;
                    })) {
                    this.ozonPriceDialogError = 'Выберите магазин из списка.';
                    this.$nextTick(function () {
                        this.$refs.ozonPriceAccount.focus();
                    });
                    return;
                }
                this.navigateOzonPriceHandoff(accountId, review);
            },
            navigateOzonPriceHandoff: function (accountId, review) {
                if (!review || this.loading || this.loadingMore || this.selectAllLoading ||
                    this._feedRevision !== review.feedRevision || this.feedScopeKey() !== review.scopeKey ||
                    this.itemsScope !== review.scopeKey ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search ||
                    (review.source === 'bulk' && !sameOrderedIds(this.selected, review.imported_product_ids)) ||
                    (review.source === 'row' && review.imported_product_ids.length !== 1) ||
                    (review.source === 'bulk' && !this.workspaceReturnScopeIsCurrent(review.returnScope))) {
                    this.closeOzonPriceHandoff();
                    this.setOzonPriceHandoffError(
                        'Выбор товаров изменился после проверки. Отметьте их заново перед сравнением.'
                    );
                    return false;
                }
                var selectedItems = this.priceWorkspaceItemsBySelection(review.imported_product_ids);
                if (!selectedItems) {
                    this.closeOzonPriceHandoff();
                    this.setOzonPriceHandoffError(
                        'Не удалось проверить точный список товаров. Обновите страницу и повторите действие.'
                    );
                    return false;
                }
                var latest = this.resolveOzonPriceAccountChoices(
                    selectedItems,
                    review.imported_product_ids,
                );
                var freshChoice = latest.choices.find(function (choice) {
                    return choice.account_id === accountId;
                });
                var reviewedChoice = review.choices.find(function (choice) {
                    return choice.account_id === accountId;
                });
                if (!freshChoice || !reviewedChoice ||
                    !sameOrderedIds(freshChoice.listing_ids, reviewedChoice.listing_ids)) {
                    this.closeOzonPriceHandoff();
                    this.setOzonPriceHandoffError(
                        'Связанные карточки изменились после проверки. Обновите список и повторите действие.'
                    );
                    return false;
                }
                if (this.priceWorkspaceDialogAvailable) {
                    this.closeOzonPriceHandoff({restoreFocus: false});
                    return this.openPriceWorkspaceDialog('ozon', review.imported_product_ids, {
                        source: review.source,
                        returnFocus: review.returnFocus,
                        choice: freshChoice,
                    });
                }
                var returnScope = review.returnScope;
                var returnNonce = storePriceWorkspaceReturn(this, {
                    channel: 'ozon',
                    ids: freshChoice.listing_ids.slice(),
                    accountId: accountId,
                }, review.imported_product_ids, returnScope);
                try {
                    if (typeof this.urls.ozonCommercial !== 'string' ||
                        !this.urls.ozonCommercial) throw new Error('missing URL');
                    var target = new URL(this.urls.ozonCommercial, window.location.origin);
                    if (target.origin !== window.location.origin) throw new Error('foreign URL');
                    target.searchParams.set('account_id', String(accountId));
                    target.searchParams.set('bulk_listing_ids', freshChoice.listing_ids.join(','));
                    target.hash = workspaceReturnHash(returnNonce).replace(/^#/, '');
                    window.location.assign(target.pathname + target.search + target.hash);
                    return true;
                } catch (_) {
                    this.closeOzonPriceHandoff();
                    this.setOzonPriceHandoffError(
                        'Не удалось открыть сравнение цен Ozon. Повторите действие со свежим выбором.'
                    );
                    return false;
                }
            },

            /* ---------- массовые действия ---------- */
            startAction: function (action, returnFocus) {
                if (this.loading || this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) {
                    this.error = 'Список обновляется. Дождитесь загрузки и выберите товары снова.';
                    return;
                }
                if (!this.selectionRowsVerified) {
                    this.error = this.selectionReviewLoading
                        ? 'Проверяем точный список выбранных товаров. Подождите завершения чтения.'
                        : 'Сначала проверьте точный список кнопкой «Показать выбранные». Действия по непроверенному выбору недоступны.';
                    return;
                }
                var chosen = this.selectedItems;
                var selectionIds = this.selected.slice();
                if (!chosen.length || selectionIds.length !== chosen.length ||
                    new Set(selectionIds).size !== selectionIds.length ||
                    chosen.some(function (item) { return selectionIds.indexOf(item.id) === -1; })) {
                    this.error = 'Выбор изменился. Обновите список и отметьте товары заново.';
                    return;
                }
                if (action && action.key === 'push_wb' &&
                    (this.wbCreateStorageProblem || this.wbCreatePending)) {
                    this.error = this.wbCreateStorageProblem
                        ? 'Сохранённый запуск WB нельзя прочитать. Новая отправка заблокирована.'
                        : 'Сначала проверьте сохранённый результат предыдущего запуска WB.';
                    return;
                }
                var published = chosen.filter(function (i) { return i.wb; }).length;
                var createIds = chosen.filter(function (item) {
                    return !item.wb && ['pending', 'failed', 'validated'].indexOf(item.import_status) !== -1;
                }).map(function (item) { return item.id; });
                var plans = {
                    push_wb: {
                        title: 'Создать карточки WB',
                        message: 'Попробуем создать карточки для ' + createIds.length + ' '
                            + this.$fmt.plural(createIds.length, 'товар', 'товара', 'товаров') + '.',
                        details: [
                            chosen.length > createIds.length
                                ? 'Остальные выбранные товары не войдут в этот запуск.'
                                : 'Запускается только создание карточек, ещё не связанных с WB.',
                            'Для ожидающих или ошибочных товаров сначала проверим готовность.',
                            'При создании отправим рассчитанную цену, если она доступна, и доступные фото.',
                            'Отправка идёт в фоне, за ней можно следить здесь',
                        ],
                        label: 'Создать карточки',
                    },
                    refresh_supplier: {
                        title: 'Обновить из поставщика',
                        message: 'Обновим ' + chosen.length + ' '
                            + this.$fmt.plural(chosen.length, 'карточку', 'карточки', 'карточек')
                            + ' данными из каталога поставщика.',
                        details: [
                            'Изменятся только данные товара в Seller Hub; карточки WB и Ozon не меняются.',
                            'Товары без связи с каталогом поставщика будут отмечены отдельно.',
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
                    var enrichIds = exactWbProductIds(chosen);
                    if (!enrichIds || enrichIds.length > MAX_BULK_SELECTION) {
                        this.error = 'Для изменения выберите только опубликованные карточки WB, не больше 200.';
                        return;
                    }
                    // Существующий review route принимает POST selected_ids с WB Product ID.
                    this.postSelectionForm(this.urls.enrichBulk, enrichIds);
                    return;
                }
                if (action.key === 'ozon') {
                    if (!selectionIds.length || selectionIds.length > MAX_BULK_SELECTION) {
                        this.ozonError = 'Выберите от 1 до 200 товаров для одной подготовки.';
                        return;
                    }
                    this.openOzonPreparation(selectionIds, returnFocus);
                    return;
                }
                if (action.key === 'common_content') {
                    var productIds = selectionIds.slice();
                    if (productIds.length > 50) {
                        this.error = 'За один раз можно открыть не больше 50 товаров. Снимите часть отметок.';
                        return;
                    }
                    this.navigateCommonContent(productIds, selectionIds);
                    return;
                }
                if (action.key === 'wb_edit') {
                    if (!selectionIds.length || selectionIds.length > MAX_BULK_SELECTION) {
                        this.error = 'Для изменения карточек WB выберите от 1 до 200 товаров.';
                        return;
                    }
                    this.openWbBulkEdit(selectionIds);
                    return;
                }
                if (action.key === 'wb_prices') {
                    var priceIds = exactWbProductIds(chosen);
                    if (!priceIds || priceIds.length > MAX_BULK_SELECTION || !this.wbConnected) {
                        this.error = 'Для изменения цены выберите до 200 карточек WB и проверьте подключение кабинета.';
                        return;
                    }
                    if (this.priceWorkspaceDialogAvailable) {
                        this.openPriceWorkspaceDialog('wb', selectionIds, {
                            source: 'bulk',
                            returnFocus: returnFocus,
                        });
                        return;
                    }
                    var returnScope = this.buildWorkspaceReturnScope();
                    var returnNonce = storePriceWorkspaceReturn(this, {
                        channel: 'wb',
                        ids: priceIds.slice(),
                        accountId: null,
                    }, selectionIds, returnScope);
                    window.location.assign(this.urls.wbPriceChange + '?product_ids=' +
                        encodeURIComponent(priceIds.join(',')) + workspaceReturnHash(returnNonce));
                    return;
                }
                if (action.key === 'ozon_workspace_update') {
                    if (!selectionIds.length || selectionIds.length > MAX_BULK_SELECTION) {
                        this.error = 'Для изменения карточек Ozon выберите от 1 до 200 товаров.';
                        return;
                    }
                    this.openOzonWorkspaceUpdate(chosen, selectionIds);
                    return;
                }
                if (action.key === 'ozon_prices') {
                    if (selectionIds.length > MAX_OZON_PRICE_HANDOFF) {
                        this.setOzonPriceHandoffError(
                            'За одно сравнение Ozon можно выбрать не больше 100 товаров.'
                        );
                        return;
                    }
                    this.openOzonPriceHandoff(chosen, selectionIds, {
                        source: 'bulk',
                        returnFocus: returnFocus,
                    });
                    return;
                }
                var plan = plans[action.key];
                if (!plan) return;
                var effectIds = action.key === 'push_wb' ? createIds : selectionIds;
                if (!effectIds.length || effectIds.length > MAX_BULK_SELECTION) {
                    this.error = 'Для одного запуска выберите допустимый набор товаров и повторите действие.';
                    return;
                }
                if (action.key === 'refresh_supplier') {
                    this.openSupplierRefreshReview(
                        effectIds,
                        selectionIds,
                        selectedProductLabels(chosen, effectIds),
                    );
                    return;
                }
                this.confirm = {
                    open: true,
                    key: action.key,
                    title: plan.title,
                    message: plan.message,
                    details: plan.details,
                    label: plan.label,
                    danger: !!plan.danger,
                    busy: false,
                    productIds: effectIds.slice(),
                    selectionIds: selectionIds.slice(),
                    labels: selectedProductLabels(chosen, effectIds),
                };
            },
            postSelectionForm: function (url, productIds, preset, fromSupplierRefresh) {
                if (preset !== undefined && preset !== 'characteristics') return false;
                if (fromSupplierRefresh && preset !== 'characteristics') return false;
                var csrf = document.querySelector('meta[name="csrf-token"]')?.content;
                if (!csrf) {
                    this.error = 'Защита формы недоступна. Обновите страницу и повторите действие.';
                    return;
                }
                var form = document.createElement('form');
                form.method = 'post';
                form.action = url;
                var csrfInput = document.createElement('input');
                csrfInput.type = 'hidden';
                csrfInput.name = 'csrf_token';
                csrfInput.value = csrf;
                form.appendChild(csrfInput);
                productIds.forEach(function (id) {
                    var input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = 'selected_ids';
                    input.value = String(id);
                    form.appendChild(input);
                });
                if (preset === 'characteristics') {
                    var presetInput = document.createElement('input');
                    presetInput.type = 'hidden';
                    presetInput.name = 'preset';
                    presetInput.value = 'characteristics';
                    form.appendChild(presetInput);
                }
                if (fromSupplierRefresh === true) {
                    var sourceInput = document.createElement('input');
                    sourceInput.type = 'hidden';
                    sourceInput.name = 'from_supplier_refresh';
                    sourceInput.value = '1';
                    form.appendChild(sourceInput);
                }
                document.body.appendChild(form);
                form.submit();
                return true;
            },
            cancelAction: function () {
                if (this.confirm.busy) return;
                this.confirm.open = false;
                this.confirm.productIds = [];
                this.confirm.selectionIds = [];
                this.confirm.labels = [];
            },
            openSupplierRefreshReview: function (ids, selectionIds, labels) {
                if (this.supplierRefreshReview &&
                    this.supplierRefreshReview.phase === 'applying') return false;
                this.cancelSupplierRefreshReview({ restoreFocus: false });
                if (!Array.isArray(ids) || !ids.length || ids.length > MAX_BULK_SELECTION ||
                    ids.some(function (id) { return !Number.isSafeInteger(id) || id <= 0; }) ||
                    new Set(ids).size !== ids.length ||
                    !sameOrderedIds(ids, selectionIds) || !sameOrderedIds(this.selected, selectionIds) ||
                    this.loading || this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) {
                    this.error = 'Список или выбор изменился. Обновите список и отметьте товары заново.';
                    return false;
                }
                var active = document.activeElement;
                var trigger = null;
                if (active && typeof active.closest === 'function') {
                    var actionRoot = active.closest('.shk-actions');
                    var detailsRoot = active.closest('.mp-more-actions');
                    trigger = actionRoot
                        ? actionRoot.querySelector('button[aria-haspopup="menu"]')
                        : detailsRoot ? detailsRoot.querySelector('summary') : null;
                }
                if (!trigger && active && active.isConnected !== false && active !== document.body) {
                    trigger = active;
                }
                this._supplierRefreshTrigger = trigger && trigger !== document.body ? trigger : null;
                var generation = (this._supplierRefreshDialogGeneration || 0) + 1;
                this._supplierRefreshDialogGeneration = generation;
                this.supplierRefreshReview = {
                    generation: generation,
                    phase: 'loading',
                    error: '',
                    requested: ids.length,
                    eligible: null,
                    changed: null,
                    skipped: null,
                    items: [],
                    reviewToken: '',
                    productIds: ids.slice(),
                    selectionIds: selectionIds.slice(),
                    labels: Array.isArray(labels) ? labels.map(function (entry) {
                        return { productId: entry.productId, label: entry.label };
                    }) : [],
                    scope: this.feedScopeKey(),
                };
                this.$nextTick(function () {
                    var dialog = this.$refs.supplierRefreshDialog;
                    var review = this.supplierRefreshReview;
                    if (!review || review.generation !== generation || !dialog) return;
                    if (!sameOrderedIds(this.selected, review.selectionIds) ||
                        this.feedScopeKey() !== review.scope || this.itemsScope !== review.scope) {
                        this.cancelSupplierRefreshReview({ restoreFocus: false });
                        return;
                    }
                    if (!dialog.open) dialog.showModal();
                    this.$refs.supplierRefreshCancel?.focus();
                });
                this.loadSupplierRefreshPreview();
                return true;
            },
            supplierRefreshReviewIsCurrent: function (review) {
                return !!review && this.supplierRefreshReview &&
                    this.supplierRefreshReview.generation === review.generation &&
                    this.feedScopeKey() === review.scope && this.itemsScope === review.scope &&
                    sameOrderedIds(this.selected, review.selectionIds);
            },
            loadSupplierRefreshPreview: function () {
                var review = this.supplierRefreshReview;
                if (!review || !this.supplierRefreshReviewIsCurrent(review)) {
                    this.cancelSupplierRefreshReview({ restoreFocus: false });
                    this.error = 'Список или выбор изменился. Отметьте товары заново и проверьте их ещё раз.';
                    return false;
                }
                if (this._supplierRefreshPreviewTimer) {
                    clearTimeout(this._supplierRefreshPreviewTimer);
                    this._supplierRefreshPreviewTimer = null;
                }
                if (this._supplierRefreshPreviewAbort) this._supplierRefreshPreviewAbort.abort();
                var self = this;
                var abort = new AbortController();
                this._supplierRefreshPreviewAbort = abort;
                var timedOut = false;
                var timer = setTimeout(function () {
                    timedOut = true;
                    abort.abort();
                }, SUPPLIER_REFRESH_PREVIEW_TIMEOUT_MS);
                this._supplierRefreshPreviewTimer = timer;
                review.phase = 'loading';
                review.error = '';
                review.eligible = null;
                review.changed = null;
                review.skipped = null;
                review.items = [];
                review.reviewToken = '';
                function isCurrent() {
                    return self._supplierRefreshPreviewAbort === abort &&
                        self.supplierRefreshReviewIsCurrent(review);
                }
                var request;
                try {
                    request = this.$api.post(
                        this.urls.supplierRefreshPreview,
                        { product_ids: review.productIds.slice() },
                        { signal: abort.signal },
                    );
                } catch (err) {
                    request = Promise.reject(err);
                }
                return Promise.resolve(request).then(function (data) {
                    if (!isCurrent()) return null;
                    if (timedOut) {
                        review.phase = 'error';
                        review.error = 'Проверка заняла больше 12 секунд. Список не менялся. Повторите проверку.';
                        self.focusSupplierRefreshReviewRetry();
                        return null;
                    }
                    var preview = validSupplierRefreshPreview(data, review.productIds);
                    if (!preview) {
                        review.phase = 'error';
                        review.error = 'Ответ не подтвердил точный состав и порядок выбранных товаров. Проверьте обновление ещё раз.';
                        self.focusSupplierRefreshReviewRetry();
                        return null;
                    }
                    review.requested = preview.requested;
                    review.eligible = preview.eligible;
                    review.changed = preview.changed;
                    review.skipped = preview.skipped;
                    review.items = preview.items;
                    review.reviewToken = preview.reviewToken;
                    review.phase = 'ready';
                    return preview;
                }).catch(function (err) {
                    if (!isCurrent()) return null;
                    review.phase = err && err.status === 409 ? 'stale' : 'error';
                    review.error = timedOut
                        ? 'Проверка заняла больше 12 секунд. Список не менялся. Повторите проверку.'
                        : err && err.status === 409
                            ? 'Список или данные товаров изменились. Проверьте обновление снова.'
                            : 'Не удалось проверить изменения. Повторите проверку перед обновлением.';
                    self.focusSupplierRefreshReviewRetry();
                    return null;
                }).finally(function () {
                    clearTimeout(timer);
                    if (self._supplierRefreshPreviewTimer === timer) {
                        self._supplierRefreshPreviewTimer = null;
                    }
                    if (self._supplierRefreshPreviewAbort === abort) {
                        self._supplierRefreshPreviewAbort = null;
                    }
                });
            },
            focusSupplierRefreshReviewRetry: function () {
                this.$nextTick(function () {
                    this.$refs.supplierRefreshReviewRetry?.focus();
                });
            },
            cancelSupplierRefreshReview: function (options) {
                var review = this.supplierRefreshReview;
                if (review && review.phase === 'applying' && !(options && options.force)) return false;
                var abort = this._supplierRefreshPreviewAbort;
                this._supplierRefreshPreviewAbort = null;
                if (this._supplierRefreshPreviewTimer) {
                    clearTimeout(this._supplierRefreshPreviewTimer);
                    this._supplierRefreshPreviewTimer = null;
                }
                this._supplierRefreshDialogGeneration = (this._supplierRefreshDialogGeneration || 0) + 1;
                if (abort) abort.abort();
                var dialog = this.$refs.supplierRefreshDialog;
                if (dialog && dialog.open) dialog.close();
                this.supplierRefreshReview = null;
                var trigger = this._supplierRefreshTrigger;
                this._supplierRefreshTrigger = null;
                if ((!options || options.restoreFocus !== false) && trigger &&
                    trigger.isConnected !== false && typeof trigger.focus === 'function') {
                    trigger.focus();
                }
                return true;
            },
            supplierRefreshValueLabel: function (value) {
                return supplierRefreshPreviewValue(value);
            },
            confirmSupplierRefreshReview: function () {
                var self = this;
                var review = this.supplierRefreshReview;
                if (!review || review.phase !== 'ready' || review.eligible < 1 ||
                    !review.reviewToken || !this.supplierRefreshReviewIsCurrent(review)) {
                    if (review && review.phase !== 'applying') {
                        review.phase = 'stale';
                        review.error = 'Список или выбор изменился. Проверьте обновление снова.';
                        review.reviewToken = '';
                        this.focusSupplierRefreshReviewRetry();
                    }
                    return false;
                }
                var ids = review.productIds.slice();
                var selectionAtReview = review.selectionIds.slice();
                var labelsAtReview = review.labels.map(function (entry) {
                    return Object.assign({}, entry);
                });
                var reviewToken = review.reviewToken;
                review.phase = 'applying';
                review.error = '';
                this.clearSupplierRefreshResult();

                var request;
                try {
                    request = this.$api.post(this.urls.refreshFromSupplier, {
                        product_ids: ids,
                        review_token: reviewToken,
                    });
                } catch (err) {
                    request = Promise.reject(err);
                }
                return Promise.resolve(request).then(function (data) {
                    var currentReview = self.supplierRefreshReview;
                    if (!currentReview || currentReview.generation !== review.generation) {
                        if (self._supplierRefreshUnmounted) {
                            var lateOutcome = supplierRefreshOutcome(
                                data, ids, labelsAtReview, self.urls.commonContent || '',
                            );
                            if (lateOutcome) {
                                saveSupplierRefreshReceipt(data.resume_receipt);
                            }
                        }
                        return null;
                    }
                    if (sameOrderedIds(self.selected, selectionAtReview)) self.selected = [];
                    var outcome = supplierRefreshOutcome(
                        data, ids, labelsAtReview, self.urls.commonContent || '',
                    );
                    self.cancelSupplierRefreshReview({ restoreFocus: false, force: true });
                    if (outcome) {
                        if (!saveSupplierRefreshReceipt(data.resume_receipt)) {
                            outcome.resumeWarning = outcome.resumeWarning || 'Этот итог нельзя будет восстановить после перехода.';
                        }
                        self.supplierRefreshResult = outcome;
                        self.load();
                        self.loadFacets();
                    } else {
                        self.supplierRefreshResult = supplierRefreshNotice(
                            'unknown', labelsAtReview.length,
                            'Ответ не подтвердил итог. Обновите список перед следующим запуском.',
                        );
                    }
                    self.$nextTick(function () {
                        self.$refs.supplierRefreshResult?.focus();
                    });
                    return outcome;
                }).catch(function (err) {
                    var currentReview = self.supplierRefreshReview;
                    if (!currentReview || currentReview.generation !== review.generation) return null;
                    if (err && err.status === 409 &&
                        err.code === 'supplier_refresh_review_stale') {
                        currentReview.phase = 'stale';
                        currentReview.reviewToken = '';
                        currentReview.error = 'Данные товаров изменились. Проверьте обновление снова.';
                        self.focusSupplierRefreshReviewRetry();
                        return null;
                    }
                    if (sameOrderedIds(self.selected, selectionAtReview)) self.selected = [];
                    var rejected = Number.isInteger(err && err.status) && err.status >= 400 &&
                        err.status < 500 && !(err && err.code === 'auth_required') &&
                        err.status !== 409;
                    self.cancelSupplierRefreshReview({ restoreFocus: false, force: true });
                    self.supplierRefreshResult = supplierRefreshNotice(
                        rejected ? 'rejected' : 'unknown',
                        labelsAtReview.length,
                        rejected
                            ? 'Сервер отклонил запрос. Обновите список и выберите товары заново.'
                            : 'Сервер не подтвердил итог. Обновите список перед следующим запуском.',
                    );
                    self.$nextTick(function () {
                        self.$refs.supplierRefreshResult?.focus();
                    });
                    return null;
                });
            },
            cancelSupplierRefreshRestore: function () {
                var abort = this._supplierRefreshRestoreAbort;
                this._supplierRefreshRestoreAbort = null;
                if (abort) abort.abort();
                this.supplierRefreshRestoreLoading = false;
            },
            focusSupplierRefreshResume: function (result) {
                this.$nextTick(function () {
                    var target = result
                        ? this.$refs.supplierRefreshResult
                        : this.$refs.supplierRefreshResume;
                    if (!target) return;
                    target.focus({ preventScroll: true });
                    target.scrollIntoView?.({ block: 'start', inline: 'nearest' });
                });
            },
            setSupplierRefreshResumeNote: function (message, retry, focus) {
                this.supplierRefreshResumeNote = message;
                this.supplierRefreshResumeRetry = !!retry;
                if (focus && this._supplierRefreshReturnAnchor) {
                    this.focusSupplierRefreshResume(false);
                }
            },
            clearSupplierRefreshResult: function () {
                this.cancelSupplierRefreshRestore();
                clearSupplierRefreshReceipt();
                this.supplierRefreshResult = null;
                this.supplierRefreshResumeNote = '';
                this.supplierRefreshResumeRetry = false;
            },
            hideSupplierRefreshResult: function () {
                this.clearSupplierRefreshResult();
                this._supplierRefreshReturnAnchor = false;
                if (window.location && window.location.hash === '#supplier-refresh-result' &&
                    window.history && typeof window.history.replaceState === 'function') {
                    try {
                        window.history.replaceState(
                            window.history.state,
                            '',
                            window.location.pathname + window.location.search,
                        );
                    } catch (_) {}
                }
            },
            restoreSupplierRefreshResult: function () {
                if ((this.importBatchMode && !this.supplierRefreshResultBatchMode) ||
                    this.supplierRefreshBatchMode) return false;
                this.cancelSupplierRefreshRestore();
                var stored = readSupplierRefreshReceipt();
                this.supplierRefreshResumeNote = '';
                this.supplierRefreshResumeRetry = false;

                if (!stored.receipt) {
                    if (this._supplierRefreshReturnAnchor) {
                        var missingMessage = stored.storageError
                            ? 'Сохранённый итог недоступен в этой вкладке.'
                            : 'Сохранённый итог обновления истёк или недоступен.';
                        this.setSupplierRefreshResumeNote(missingMessage, false, true);
                    }
                    return false;
                }

                var receipt = stored.receipt;
                var resultUrl = this.urls && this.urls.supplierRefreshResult;
                if (typeof resultUrl !== 'string' || !resultUrl) {
                    this.setSupplierRefreshResumeNote(
                        'Не удалось проверить сохранённый итог. Повторите чтение.', true, true,
                    );
                    return false;
                }
                try {
                    var parsedResultUrl = new URL(resultUrl, window.location.origin);
                    if (parsedResultUrl.origin !== window.location.origin ||
                        parsedResultUrl.username || parsedResultUrl.password || parsedResultUrl.hash) {
                        throw new Error('invalid result URL');
                    }
                    resultUrl = parsedResultUrl.pathname + parsedResultUrl.search;
                } catch (_) {
                    this.setSupplierRefreshResumeNote(
                        'Не удалось проверить сохранённый итог. Обновите страницу и повторите чтение.', false, true,
                    );
                    return false;
                }

                var self = this;
                var abort = new AbortController();
                this._supplierRefreshRestoreAbort = abort;
                this.supplierRefreshRestoreLoading = true;
                this.supplierRefreshResumeNote = 'Загружаем сохранённый итог обновления…';
                this.supplierRefreshResumeRetry = false;
                var timer = setTimeout(function () { abort.abort(); }, 10000);
                var isCurrent = function () {
                    return self._supplierRefreshRestoreAbort === abort;
                };
                var transientMessage = 'Не удалось загрузить сохранённый итог. Повторите чтение.';
                return fetch(resultUrl, {
                    method: 'GET',
                    credentials: 'same-origin',
                    cache: 'no-store',
                    redirect: 'manual',
                    signal: abort.signal,
                    headers: {
                        Accept: 'application/json',
                        'X-Supplier-Refresh-Receipt': receipt,
                    },
                }).then(function (response) {
                    if (!isCurrent()) return null;
                    if ([400, 404, 410].indexOf(response.status) !== -1) {
                        clearSupplierRefreshReceipt(receipt);
                        self.supplierRefreshRestoreLoading = false;
                        if (self._supplierRefreshReturnAnchor) {
                            self.setSupplierRefreshResumeNote(
                                'Сохранённый итог обновления истёк или недоступен.', false, true,
                            );
                        } else {
                            self.supplierRefreshResumeNote = '';
                        }
                        return null;
                    }
                    if (!response.ok || response.redirected || response.type === 'opaqueredirect' ||
                        !(response.headers?.get('content-type') || '').includes('application/json')) {
                        self.setSupplierRefreshResumeNote(transientMessage, true, true);
                        return null;
                    }
                    return response.json().then(function (data) {
                        if (!isCurrent()) return null;
                        var outcome = restoredSupplierRefreshOutcome(
                            data, self.urls.commonContent || '',
                        );
                        if (!outcome) {
                            self.setSupplierRefreshResumeNote(transientMessage, true, true);
                            return null;
                        }
                        self.supplierRefreshResult = outcome;
                        self.supplierRefreshResumeNote = '';
                        self.supplierRefreshResumeRetry = false;
                        if (self._supplierRefreshReturnAnchor) {
                            self.focusSupplierRefreshResume(true);
                        }
                        return outcome;
                    });
                }).catch(function () {
                    if (!isCurrent()) return null;
                    self.setSupplierRefreshResumeNote(transientMessage, true, true);
                    return null;
                }).finally(function () {
                    clearTimeout(timer);
                    if (self._supplierRefreshRestoreAbort === abort) {
                        self._supplierRefreshRestoreAbort = null;
                        self.supplierRefreshRestoreLoading = false;
                    }
                });
            },
            runAction: function () {
                var self = this;
                if (this.confirm.busy || !this.confirm.open) return;
                var key = this.confirm.key;
                var ids = Array.isArray(this.confirm.productIds) ? this.confirm.productIds.slice() : [];
                var selectionAtReview = Array.isArray(this.confirm.selectionIds) ? this.confirm.selectionIds.slice() : [];
                if (!key || !ids.length || ids.length > MAX_BULK_SELECTION) return;
                if (!sameIds(this.selected, selectionAtReview)) {
                    this.confirm.open = false;
                    this.confirm.productIds = [];
                    this.confirm.selectionIds = [];
                    this.confirm.labels = [];
                    this.error = 'Выбор изменился после проверки. Отметьте нужные товары и откройте действие заново.';
                    return;
                }
                this.confirm.busy = true;

                if (key === 'push_wb' && this.importBatchMode && this.importBatchKind === 'import') {
                    if (!this.verifiedSignedImportBatch || this.wbCreateStorageProblem || this.wbCreatePending) {
                        this.confirm.open = false;
                        this.error = 'Сначала подтвердите точный состав этого импорта и проверьте незавершённый запуск WB.';
                        this.finishConfirmAction();
                        return;
                    }
                    this.startInlineWbCreate(ids, selectionAtReview);
                    return;
                }

                var request;
                if (key === 'push_wb') {
                    if (this.inlineSupplierImportBatch) {
                        this.startInlineWbCreate(ids, selectionAtReview);
                        return;
                    }
                    this.jobTitle = 'Отправляем на Wildberries';
                    request = this.$api.post(this.urls.pushToWb, { product_ids: ids });
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
                    if (sameIds(self.selected, selectionAtReview)) self.selected = [];
                    if (data.job_uid || data.job_id) {
                        self.trackJob(data.job_uid || data.job_id, ids.length);
                        self.focusJobProgress();
                    } else {
                        self.stopJobTracking();
                        var updated = typeof data.updated === 'number' ? data.updated : null;
                        var deleted = typeof data.deleted === 'number' ? data.deleted : null;
                        var skipped = typeof data.skipped === 'number' ? data.skipped : 0;
                        var succeeded = updated !== null ? updated : (deleted !== null ? deleted : ids.length);
                        var processed = updated !== null ? updated + skipped : ids.length;
                        // Синхронное действие: показываем итог сразу
                        self.job = {
                            status: 'done',
                            total: ids.length,
                            processed: processed,
                            succeeded: succeeded,
                            skipped: skipped,
                            failed: typeof data.errors === 'number' ? data.errors : 0,
                            message: data.message || '',
                        };
                        self.load();
                        self.loadFacets();
                        self.focusJobProgress();
                    }
                }).catch(function (err) {
                    self.confirm.open = false;
                    self.error = err.message || 'Действие не выполнено';
                }).finally(function () {
                    self.confirm.busy = false;
                    self.confirm.productIds = [];
                    self.confirm.selectionIds = [];
                    self.confirm.labels = [];
                });
            },
            finishConfirmAction: function () {
                this.confirm.busy = false;
                this.confirm.productIds = [];
                this.confirm.selectionIds = [];
                this.confirm.labels = [];
            },
            markWbCreateSelectionIntent: function () {
                if (!this.wbCreatePending) return;
                var self = this;
                var revision = this.wbCreateSelectionIntentRevision + 1;
                this.wbCreateSelectionIntentRevision = revision;
                var immediate = Object.assign({}, this.wbCreatePending, {
                    selection_changed: true,
                    current_selection_ids: this.selected.slice(),
                });
                this.wbCreatePending = immediate;
                if (!saveWbCreatePending(immediate)) {
                    this.wbCreateStorageProblem = true;
                    this.wbCreateReadError = 'Не удалось сохранить новый выбор. Результат WB остаётся защищённым.';
                }
                this.$nextTick(function () {
                    if (!self.wbCreatePending || self.wbCreateSelectionIntentRevision !== revision) return;
                    var pending = Object.assign({}, self.wbCreatePending, {
                        current_selection_ids: self.selected.slice(),
                    });
                    if (!saveWbCreatePending(pending)) {
                        self.wbCreateStorageProblem = true;
                        self.wbCreateReadError = 'Не удалось сохранить новый выбор. Результат WB остаётся защищённым.';
                    }
                    self.wbCreatePending = pending;
                });
            },
            clearSelection: function () {
                this.selected = [];
                this.markWbCreateSelectionIntent();
                this.cancelSelectionReview({ clearRows: true });
                this.$nextTick(function () {
                    this.focusCatalogAfterSelectionReview();
                });
            },
            wbCreateActionUrl: function (kind) {
                var value = kind === 'result' ? this.urls.wbCreateResult : this.urls.pushToWb;
                var suffix = kind === 'result'
                    ? '/my-products/push-to-wb-bulk/result'
                    : '/my-products/push-to-wb-bulk';
                if (typeof value !== 'string' || !value || value.length > 2000) return '';
                try {
                    var target = new URL(value, window.location.origin);
                    if (target.origin !== window.location.origin || target.username || target.password ||
                        target.search || target.hash || !target.pathname.endsWith(suffix)) return '';
                    return target.pathname;
                } catch (_) { return ''; }
            },
            wbCreatePendingMatchesCurrentBatch: function (pending) {
                return !!pending && pending.owner === this.workspaceOwner && this.importBatchMode &&
                    this.importBatchKind === 'import' && this.importBatch === pending.import_batch &&
                    !this.importBatchInvalid;
            },
            wbCreateFeedContains: function (ids) {
                if (!validWbCreateIds(ids, true) || !this.verifiedSignedImportBatch &&
                    !this.wbCreatePendingMatchesCurrentBatch(this.wbCreatePending)) return false;
                var feedIds = orderedProductIds(this.items);
                return !!feedIds && ids.every(function (id) { return feedIds.indexOf(id) !== -1; });
            },
            saveCurrentWbCreatePending: function (pending) {
                if (!pending || !validWbCreatePending(pending)) return false;
                this.wbCreatePending = pending;
                if (!saveWbCreatePending(pending)) {
                    this.wbCreateStorageProblem = true;
                    this.wbCreateReadError = 'Не удалось обновить сохранённую запись WB. Новая отправка заблокирована.';
                    return false;
                }
                return true;
            },
            startInlineWbCreate: function (productIds, selectionIds) {
                if (!this.verifiedSignedImportBatch || this.wbCreateStorageProblem || this.wbCreatePending ||
                    !Array.isArray(productIds) || !validWbCreateIds(productIds, false) ||
                    !validWbCreateIds(selectionIds, false) || !sameOrderedIds(this.selected, selectionIds) ||
                    productIds.some((id) => selectionIds.indexOf(id) === -1) ||
                    !this.wbCreateFeedContains(productIds) || !this.wbCreateFeedContains(selectionIds)) {
                    this.confirm.open = false;
                    this.error = 'WB-запуск остановлен: проверьте точный состав выбранного импорта и повторите действие.';
                    this.finishConfirmAction();
                    return false;
                }
                var requestKey = wbCreateRequestKey();
                if (!requestKey || !this.wbCreateActionUrl('post') || !this.wbCreateActionUrl('result') ||
                    !document.querySelector('meta[name="csrf-token"]')?.content) {
                    this.confirm.open = false;
                    this.error = 'Не удалось безопасно подготовить запрос WB. Ничего не отправлено.';
                    this.finishConfirmAction();
                    return false;
                }
                var pending = {
                    version: 1,
                    owner: this.workspaceOwner,
                    import_batch: this.importBatch,
                    request_key: requestKey,
                    selection_ids: selectionIds.slice(),
                    product_ids: productIds.slice(),
                    current_selection_ids: selectionIds.slice(),
                    selection_changed: false,
                    state: 'posting',
                    job_uid: null,
                    receipt_status: null,
                    worker_exited: false,
                    created_at: Date.now(),
                };
                if (!saveWbCreatePending(pending)) {
                    this.wbCreateStorageProblem = true;
                    this.confirm.open = false;
                    this.error = 'Не удалось сохранить ключ WB в этой вкладке. Запрос не отправлен.';
                    this.finishConfirmAction();
                    return false;
                }
                this.wbCreatePending = pending;
                this.wbCreateTerminalResult = null;
                this.wbCreateReceiptFocusSet = false;
                this.wbCreateStorageProblem = false;
                this.wbCreateRetrySameKey = false;
                this.wbCreateError = '';
                this.wbCreateReadError = '';
                this.jobTitle = 'Запускаем создание карточек WB…';
                this.job = null;
                this.confirm.open = false;
                this.postInlineWbCreate(pending, false);
                return true;
            },
            postInlineWbCreate: function (pending, explicitRetry) {
                if (!pending || !validWbCreatePending(pending) || this.wbCreateBusy ||
                    !this.wbCreatePending || this.wbCreatePending.request_key !== pending.request_key ||
                    (explicitRetry && (!this.wbCreateRetrySameKey || !this.wbCreatePendingMatchesCurrentBatch(pending) ||
                        !this.importBatchSelectionVerified || !this.wbCreateFeedContains(pending.product_ids)))) return false;
                var url = this.wbCreateActionUrl('post');
                var csrfMeta = document.querySelector('meta[name="csrf-token"]');
                if (!url || !csrfMeta || !csrfMeta.content) {
                    this.wbCreateReadError = 'Не удалось проверить защищённый адрес WB. Запрос не отправлен.';
                    return false;
                }
                var self = this;
                var controller = new AbortController();
                var timer = setTimeout(function () { controller.abort(); }, 20000);
                this._wbCreatePostAbort = controller;
                this._wbCreatePostTimer = timer;
                this.wbCreateBusy = true;
                this.wbCreateRetrySameKey = false;
                var posting = Object.assign({}, pending, {
                    state: 'posting', job_uid: null, receipt_status: null, worker_exited: false,
                });
                if (!this.saveCurrentWbCreatePending(posting)) {
                    this.wbCreateBusy = false;
                    clearTimeout(timer);
                    this._wbCreatePostTimer = null;
                    this._wbCreatePostAbort = null;
                    this.wbCreateReadError = 'Ключ не удалось сохранить перед отправкой. Запрос не отправлен.';
                    this.finishConfirmAction();
                    return false;
                }
                fetch(url, {
                    method: 'POST',
                    credentials: 'same-origin',
                    cache: 'no-store',
                    redirect: 'manual',
                    signal: controller.signal,
                    headers: {
                        'Content-Type': 'application/json',
                        Accept: 'application/json',
                        'X-CSRFToken': csrfMeta.content,
                    },
                    body: JSON.stringify({
                        product_ids: pending.product_ids,
                        request_key: pending.request_key,
                    }),
                }).then(function (response) {
                    return response.json().catch(function () { return null; }).then(function (data) {
                        if (self._mpUnmounted) return;
                        if (knownWbCreateNotAccepted(response, data)) {
                            self.clearKnownNotAcceptedWbCreate(pending, data);
                            return;
                        }
                        if (![200, 202].includes(response.status) ||
                            !validWbCreateReceipt(data, pending)) {
                            self.markWbCreateUnknown(pending, 'Ответ WB не подтвердил точный запуск. Проверьте результат по сохранённому ключу.');
                            self._wbCreateReadAfterPost = true;
                            return;
                        }
                        self.confirm.open = false;
                        self.applyWbCreateReceipt(pending, data);
                    });
                }).catch(function () {
                    if (self._mpUnmounted) return;
                    self.markWbCreateUnknown(pending, 'Ответ на отправку WB не получен. Сначала проверьте результат по сохранённому ключу.');
                    self._wbCreateReadAfterPost = true;
                }).finally(function () {
                    clearTimeout(timer);
                    if (self._wbCreatePostTimer === timer) self._wbCreatePostTimer = null;
                    if (self._wbCreatePostAbort === controller) self._wbCreatePostAbort = null;
                    self.wbCreateBusy = false;
                    self.finishConfirmAction();
                    if (!self._mpUnmounted && self._wbCreateReadAfterPost) {
                        self._wbCreateReadAfterPost = false;
                        self.checkWbCreateResult(true);
                    }
                });
                return true;
            },
            clearKnownNotAcceptedWbCreate: function (pending, data) {
                clearWbCreatePending(pending.request_key);
                this.wbCreatePending = null;
                this.wbCreateRetrySameKey = false;
                this.wbCreateStorageProblem = false;
                this.wbCreateError = typeof data.error === 'string' ? data.error.slice(0, 300) :
                    'Запуск WB не создан. Проверьте сообщение и при необходимости откройте новое подтверждение.';
                this.wbCreateReadError = '';
                this.job = null;
                this.confirm.open = false;
            },
            markWbCreateUnknown: function (pending, message) {
                if (!this.wbCreatePending || this.wbCreatePending.request_key !== pending.request_key) return;
                var next = Object.assign({}, this.wbCreatePending, {
                    state: 'unknown',
                    receipt_status: 'unknown',
                    worker_exited: false,
                });
                this.wbCreateTerminalReceiptVerified = false;
                this.saveCurrentWbCreatePending(next);
                this.wbCreateRetrySameKey = false;
                this.wbCreateError = message;
                this.job = null;
                this.stopWbCreateReceiptTracking();
            },
            applyWbCreateReceipt: function (pending, data) {
                if (!validWbCreateReceipt(data, pending) || !this.wbCreatePending ||
                    this.wbCreatePending.request_key !== pending.request_key) {
                    this.markWbCreateUnknown(pending, 'Получен ответ о другом составе. Запуск остаётся неизвестным; проверьте ключ.');
                    return false;
                }
                var terminal = ['completed', 'failed', 'cancelled'].includes(data.status) && data.worker_exited === true;
                this.wbCreateTerminalReceiptVerified = terminal;
                this.wbCreateTerminalResult = terminal ? {
                    status: data.status,
                    total: data.total,
                    counts: wbCreateReceiptCounts(data, data.total),
                } : null;
                var next = Object.assign({}, this.wbCreatePending, {
                    state: terminal ? 'terminal_refresh' :
                        data.status === 'unknown' || (['completed', 'failed', 'cancelled'].includes(data.status) && !data.worker_exited)
                            ? 'unknown' : 'accepted',
                    job_uid: data.job_uid,
                    receipt_status: data.status,
                    worker_exited: data.worker_exited,
                });
                this.saveCurrentWbCreatePending(next);
                this.wbCreateRetrySameKey = false;
                this.wbCreateError = terminal
                    ? 'Локальная задача завершена. Проверяем точный список импорта перед восстановлением выбора.'
                    : data.status === 'unknown' || (['completed', 'failed', 'cancelled'].includes(data.status) && !data.worker_exited)
                        ? 'Результат WB пока не подтверждён. Повторно проверьте его перед новым запуском.'
                        : '';
                this.wbCreateReadError = '';
                this.jobTitle = 'Итог локальной задачи WB';
                this.job = null;
                this.revealWbCreateReceipt();
                if (terminal) {
                    this.stopWbCreateReceiptTracking();
                    if (this.wbCreatePendingMatchesCurrentBatch(next) && !this.loading) this.load();
                } else if (next.state === 'accepted') {
                    this.scheduleWbCreateReceiptPoll();
                } else {
                    this.stopWbCreateReceiptTracking();
                }
                return true;
            },
            revealWbCreateReceipt: function () {
                if (this.wbCreateReceiptFocusSet) return;
                this.wbCreateReceiptFocusSet = true;
                this.$nextTick(function () {
                    var receipt = document.querySelector('[data-wb-create-receipt]');
                    if (!receipt) return;
                    receipt.setAttribute('tabindex', '-1');
                    receipt.focus({ preventScroll: true });
                    var rect = receipt.getBoundingClientRect();
                    var header = document.querySelector('.mobile-header');
                    var headerStyle = header ? window.getComputedStyle(header) : null;
                    var headerBottom = header && headerStyle && headerStyle.position === 'fixed'
                        ? header.getBoundingClientRect().bottom : 0;
                    var topInset = Math.max(0, headerBottom) + 12;
                    if (rect.top < topInset || rect.bottom > window.innerHeight) {
                        var targetTop = Math.max(0, window.scrollY + rect.top - topInset);
                        window.scrollTo({ top: targetTop, behavior: 'instant' });
                    }
                });
            },
            checkWbCreateResult: function (quiet) {
                var pending = this.wbCreatePending;
                if (!pending || !validWbCreatePending(pending) || this.wbCreateReading || this.wbCreateBusy) return false;
                var url = this.wbCreateActionUrl('result');
                if (!url) {
                    this.wbCreateReadError = 'Не удалось проверить адрес результата WB.';
                    return false;
                }
                var self = this;
                var controller = new AbortController();
                var timer = setTimeout(function () { controller.abort(); }, 10000);
                this._wbCreateReadAbort = controller;
                this._wbCreateReadTimer = timer;
                this.wbCreateReading = true;
                this.wbCreateRetrySameKey = false;
                fetch(url, {
                    method: 'GET',
                    credentials: 'same-origin',
                    cache: 'no-store',
                    redirect: 'manual',
                    signal: controller.signal,
                    headers: { Accept: 'application/json', 'X-WB-Create-Key': pending.request_key },
                }).then(function (response) {
                    return response.json().catch(function () { return null; }).then(function (data) {
                        if (self._mpUnmounted) return;
                        if (response.status === 404 && data && data.success === false &&
                            data.code === 'wb_create_receipt_not_found') {
                            if (pending.job_uid || (self.wbCreatePending && self.wbCreatePending.job_uid)) {
                                self.markWbCreateUnknown(pending,
                                    'Уже подтверждённый WB запуск больше не найден по ключу. Новый запуск заблокирован.');
                                self.wbCreateReadError = self.wbCreateError;
                                return;
                            }
                            var retryable = Object.assign({}, self.wbCreatePending, { state: 'unknown' });
                            self.saveCurrentWbCreatePending(retryable);
                            self.wbCreateRetrySameKey = true;
                            self.wbCreateReadError = 'Сохранённый ключ пока не найден. Можно вручную повторить только тот же запрос и набор товаров.';
                            if (!quiet) self.wbCreateError = '';
                            return;
                        }
                        if (!response.ok || !validWbCreateReceipt(data, pending)) {
                            self.markWbCreateUnknown(pending,
                                response.status === 401 || response.status === 403
                                    ? 'Проверьте вход и доступ. Сохранённый WB-запуск не повторяется автоматически.'
                                    : 'Не удалось подтвердить точный результат WB. Повторите только чтение.');
                            self.wbCreateReadError = self.wbCreateError;
                            return;
                        }
                        self.applyWbCreateReceipt(pending, data);
                    });
                }).catch(function () {
                    if (self._mpUnmounted) return;
                    self.markWbCreateUnknown(pending,
                        'Проверка WB не завершилась. Повторите чтение; новый запуск не отправлен.');
                    self.wbCreateReadError = self.wbCreateError;
                }).finally(function () {
                    clearTimeout(timer);
                    if (self._wbCreateReadTimer === timer) self._wbCreateReadTimer = null;
                    if (self._wbCreateReadAbort === controller) self._wbCreateReadAbort = null;
                    self.wbCreateReading = false;
                });
                return true;
            },
            retryWbCreateSameKey: function () {
                if (!this.wbCreateRetryAvailable) return false;
                var pending = this.wbCreatePending;
                this.wbCreateReadError = '';
                this.wbCreateError = 'Повторяем тот же запуск с прежним ключом и точным набором товаров.';
                return this.postInlineWbCreate(pending, true);
            },
            scheduleWbCreateReceiptPoll: function () {
                this.stopWbCreateReceiptTracking();
                var self = this;
                this._wbCreatePollTimer = setInterval(function () {
                    if (document.hidden || !self.wbCreatePending || self.wbCreatePending.state !== 'accepted') return;
                    self.checkWbCreateResult(true);
                }, 2500);
            },
            stopWbCreateReceiptTracking: function () {
                if (this._wbCreatePollTimer) clearInterval(this._wbCreatePollTimer);
                this._wbCreatePollTimer = null;
                if (this._wbCreateReadTimer) clearTimeout(this._wbCreateReadTimer);
                this._wbCreateReadTimer = null;
                if (this._wbCreateReadAbort) this._wbCreateReadAbort.abort();
                this._wbCreateReadAbort = null;
            },
            tryResumeWbCreateAfterFeed: function () {
                var pending = this.wbCreatePending;
                if (!pending || !this.wbCreatePendingMatchesCurrentBatch(pending) ||
                    !this.importBatchSelectionVerified || this.itemsScope !== this.feedScopeKey()) return false;
                if (!this.wbCreateFeedContains(pending.product_ids)) {
                    this.wbCreateReadError = 'Запрошенные товары не совпали с проверенным импортом. Защита WB сохранена.';
                    return false;
                }
                var desired = pending.selection_changed
                    ? pending.current_selection_ids.slice() : pending.selection_ids.slice();
                if (!this.wbCreateFeedContains(desired)) {
                    this.wbCreateReadError = 'Выбор не восстановлен: часть товаров больше не входит в эту подборку.';
                } else {
                    this.selected = desired;
                }
                if (pending.state === 'terminal_refresh' && pending.worker_exited === true &&
                    this.wbCreateTerminalReceiptVerified) {
                    this.finishWbCreateAfterVerifiedFeed(pending);
                } else if (pending.state === 'accepted' || pending.state === 'unknown' ||
                    pending.state === 'posting' || pending.state === 'terminal_refresh') {
                    this.checkWbCreateResult(true);
                }
                return true;
            },
            finishWbCreateAfterVerifiedFeed: function (pending) {
                if (!pending || !this.wbCreatePendingMatchesCurrentBatch(pending) ||
                    pending.state !== 'terminal_refresh' || pending.worker_exited !== true ||
                    !this.wbCreateTerminalReceiptVerified ||
                    !this.importBatchSelectionVerified || this.itemsScope !== this.feedScopeKey() ||
                    !this.wbCreateFeedContains(pending.product_ids)) {
                    this.wbCreateReadError = 'Не удалось подтвердить точный состав после завершения WB. Защита запуска остаётся активной.';
                    return false;
                }
                var desired = pending.selection_changed
                    ? pending.current_selection_ids.slice() : pending.selection_ids.slice();
                if (this.wbCreateFeedContains(desired)) {
                    this.selected = desired;
                } else {
                    this.selected = [];
                    this.wbCreateReadError = 'Запуск завершён, но прежний выбор изменился. Отметьте товары из проверенного списка заново.';
                }
                clearWbCreatePending(pending.request_key);
                this.wbCreatePending = null;
                this.wbCreateRetrySameKey = false;
                this.wbCreateStorageProblem = false;
                this.wbCreateTerminalReceiptVerified = false;
                this.wbCreateError = '';
                this.stopWbCreateReceiptTracking();
                return true;
            },
            dismissWbCreateTerminalResult: function () {
                if (this.wbCreatePending) return;
                this.wbCreateTerminalResult = null;
            },
            refreshListAfterSupplierResult: function () {
                this.load();
                this.loadFacets();
            },
            prepareRefreshedForOzon: function (event) {
                var productIds = this.supplierRefreshOzonIds;
                if (!productIds.length) return;

                return this.openOzonPreparation(
                    productIds,
                    event && event.currentTarget,
                );
            },
            checkRefreshedWbCharacteristics: function () {
                var productIds = this.supplierRefreshWbProductIds;
                if (!productIds.length ||
                    typeof this.urls.enrichBulk !== 'string' || !this.urls.enrichBulk) return false;

                return this.postSelectionForm(
                    this.urls.enrichBulk,
                    productIds.slice(),
                    'characteristics',
                    true,
                );
            },
            openOzonPreparation: function (productIds, returnFocus) {
                if (!this.ozonEnabled || this.ozonPreparationDisabled) return false;
                if (!Array.isArray(productIds) || !productIds.length ||
                    productIds.length > MAX_BULK_SELECTION ||
                    productIds.some(function (id) {
                        return !Number.isSafeInteger(id) || id <= 0;
                    }) || new Set(productIds).size !== productIds.length) {
                    this.ozonError = 'Не удалось проверить список товаров. Обновите его и повторите действие.';
                    this.$nextTick(function () {
                        this.$refs.ozonError?.focus();
                    });
                    return false;
                }
                this.ozonIds = productIds.slice();
                this.ozonInlineContextAtOpen = this.buildInlineOzonContext(productIds);
                this.ozonAccountId = this.ozonAccounts.length === 1
                    ? String(this.ozonAccounts[0].id) : '';
                this.ozonSelectionAtOpen = Array.isArray(this.selected)
                    ? this.selected.slice() : null;
                this.ozonSelectionScopeAtOpen = this.feedScopeKey();
                this.ozonError = '';
                this.ozonReturnFocus = returnFocus || document.activeElement || null;
                this.$nextTick(function () {
                    this.$refs.ozonDialog.showModal();
                    if (this.ozonAccounts.length === 1) {
                        this.$refs.ozonDialogTitle?.focus({ preventScroll: true });
                    } else {
                        this.$refs.ozonAccount?.focus({ preventScroll: true });
                    }
                });
                return true;
            },
            closeOzonDialog: function () {
                var dialog = this.$refs.ozonDialog;
                if (this.ozonBusy || !dialog || !dialog.open) return;
                dialog.close();
                this.ozonSelectionAtOpen = null;
                this.ozonSelectionScopeAtOpen = '';
                if (this.ozonReturnFocus && this.ozonReturnFocus.isConnected &&
                    !this.ozonReturnFocus.disabled && typeof this.ozonReturnFocus.focus === 'function') {
                    this.ozonReturnFocus.focus();
                } else if (this.ozonInlineReceiptFocusPending) {
                    this.$nextTick(function () {
                        this.$refs.ozonInlineReceipt?.focus({ preventScroll: true });
                    });
                }
                this.ozonInlineReceiptFocusPending = false;
                this.ozonInlineContextAtOpen = null;
                this.ozonReturnFocus = null;
            },
            clearOzonStorageProblem: function () {
                if (!this.ozonStorageProblem || this.ozonBusy) return;
                clearOzonPending(); this.ozonStorageProblem = false;
                this.ozonError = 'Повреждённая запись снята после проверки истории. Выберите товары заново перед подготовкой.';
            },
            buildInlineOzonContext: function (productIds) {
                if (!this.inlineSupplierImportBatch || !Array.isArray(productIds) ||
                    !sameOrderedIds(productIds, this.selected) ||
                    this.selectedItems.length !== productIds.length) return null;
                var feedIds = orderedProductIds(this.items);
                if (!feedIds || productIds.some(function (id) { return feedIds.indexOf(id) === -1; })) return null;
                var byId = new Map((this.items || []).map(function (item) { return [item.id, item]; }));
                var labels = productIds.map(function (id) {
                    var item = byId.get(id);
                    var label = item && (item.title || item.vendor_code);
                    label = typeof label === 'string' ? label.replace(/\s+/g, ' ').trim().slice(0, 180) : '';
                    return { id: id, label: label || ('Товар #' + id) };
                });
                var context = {
                    owner: this.workspaceOwner,
                    import_batch: this.importBatch,
                    selection_ids: productIds.slice(),
                    product_labels: labels,
                };
                return validInlineOzonContext(context, productIds) ? context : null;
            },
            saveInlineOzonPreparation: function (pending, run) {
                var context = pending && pending.inline_import_context;
                if (!validInlineOzonContext(context, pending && pending.imported_product_ids) ||
                    !run || run.mode !== 'source_prepare' ||
                    run.account_id !== pending.account_id ||
                    !Number.isSafeInteger(run.summary && run.summary.total) ||
                    run.summary.total !== pending.imported_product_ids.length ||
                    !/^ozon-upload-[0-9a-f]{32}$/.test(run.job_uid)) return false;
                var account = this.ozonAccounts.find(function (row) { return row.id === pending.account_id; });
                var accountLabel = account && typeof account.label === 'string'
                    ? account.label.trim().slice(0, 120) : '';
                var receipt = {
                    version: 1,
                    owner: context.owner,
                    import_batch: context.import_batch,
                    latest: {
                        account_id: pending.account_id,
                        account_label: accountLabel || ('Магазин Ozon №' + pending.account_id),
                        product_ids: context.selection_ids.slice(),
                        product_labels: context.product_labels.map(function (row) {
                            return { id: row.id, label: row.label };
                        }),
                        job_uid: run.job_uid,
                    },
                };
                if (!validOzonInlineReceipt(receipt)) return false;
                this.ozonInlineReceipt = receipt;
                this.ozonInlineReceiptStorageProblem = !saveOzonInlineReceipt(receipt);
                this.ozonInlineReceiptFocusPending = true;
                return true;
            },
            completeOzonPreparation: function (pending, run) {
                if (!run || run.mode !== 'source_prepare' || run.account_id !== pending.account_id ||
                    !/^ozon-upload-[0-9a-f]{32}$/.test(run.job_uid) ||
                    (pending.inline_import_context &&
                        (!Number.isSafeInteger(run.summary && run.summary.total) ||
                            run.summary.total !== pending.imported_product_ids.length))) {
                    throw Error('Ответ подготовки не совпал с точным набором. Проверьте результат по ключу.');
                }
                var location = ozonRunLocation(this.urls.ozonUploads, run.job_uid);
                if (!location) throw Error('Ссылка на принятый запуск не прошла проверку. Проверьте историю запусков.');
                bindOzonPreparationWorkspaceReturn(pending, run.job_uid);
                if (pending.inline_import_context) {
                    if (!this.saveInlineOzonPreparation(pending, run)) {
                        throw Error('Запуск принят, но его точный состав не прошёл проверку. Проверьте историю запусков.');
                    }
                    clearOzonPending();
                    this.ozonPending = null;
                    this.ozonRetrySameKey = false;
                    return true;
                }
                clearOzonPending();
                this.ozonPending = null;
                window.location.assign(location);
                return true;
            },
            prepareOzon: async function () {
                var dialog = this.$refs.ozonDialog;
                if (!dialog || dialog.open !== true) return;
                if (!Array.isArray(this.ozonSelectionAtOpen) ||
                    !sameOrderedIds(this.selected, this.ozonSelectionAtOpen) ||
                    this.feedScopeKey() !== this.ozonSelectionScopeAtOpen) {
                    this.closeOzonDialog();
                    this.ozonError = 'Список или выбор изменился. Отметьте товары заново перед подготовкой.';
                    this.$nextTick(function () { this.$refs.ozonError?.focus(); });
                    return;
                }
                var accountId = Number(this.ozonAccountId);
                if (this.ozonBusy || this.ozonPending || this.ozonStorageProblem || this.ozonSessionEnded ||
                    !Number.isSafeInteger(accountId) ||
                    !this.ozonAccounts.some(function (row) { return row.id === accountId; }) ||
                    !this.ozonIds.length || this.ozonIds.length > 200) return;
                var csrf = document.querySelector('meta[name="csrf-token"]')?.content;
                if (!csrf) { this.ozonError = 'Защита формы недоступна. Обновите страницу перед подготовкой.'; return; }
                var pending = {account_id:accountId, imported_product_ids:this.ozonIds.slice(),
                    request_key:ozonRequestKey()};
                if (this.ozonInlineContextAtOpen &&
                    validInlineOzonContext(this.ozonInlineContextAtOpen, pending.imported_product_ids)) {
                    pending.inline_import_context = this.ozonInlineContextAtOpen;
                }
                var selectionIds = Array.isArray(this.selected) ? this.selected.slice() : [];
                var returnScope = null;
                var returnNonce = null;
                try {
                    returnScope = this.buildWorkspaceReturnScope();
                    returnNonce = storeOzonPreparationWorkspaceReturn(
                        this, accountId, pending.imported_product_ids, selectionIds, returnScope,
                    );
                } catch (_) {
                    returnNonce = null;
                }
                if (returnNonce) pending.workspace_return_nonce = returnNonce;
                if (!saveOzonPending(pending)) {
                    if (returnNonce) this.consumeWorkspaceReturn(returnNonce);
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
                        body:JSON.stringify({
                            account_id:pending.account_id,
                            imported_product_ids:pending.imported_product_ids.slice(),
                            request_key:pending.request_key,
                            confirm_prepare:true,
                        }),
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
                    this.completeOzonPreparation(pending, data.run);
                } catch (error) {
                    if (error.status === 401 || error.status === 403) this.ozonSessionEnded = true;
                    this.ozonError = error.name === 'AbortError'
                        ? 'Ответ не пришёл вовремя. Повторная подготовка заблокирована; проверьте результат.'
                        : error instanceof TypeError
                            ? 'Связь прервалась. Повторная подготовка заблокирована; проверьте результат.'
                            : error.message;
                } finally {
                    clearTimeout(timer); this.ozonBusy = '';
                    this.closeOzonDialog();
                    if (this.ozonError) this.$nextTick(function () {
                        this.$refs.ozonError?.focus();
                    });
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
                    this.completeOzonPreparation(this.ozonPending, data.run);
                } catch (error) {
                    if (error.status === 401 || error.status === 403) this.ozonSessionEnded = true;
                    if (error.status === 404) this.ozonRetrySameKey = true;
                    this.ozonError = error.name === 'AbortError' ? 'Проверка заняла слишком много времени. Повторите только чтение.' :
                        error instanceof TypeError ? 'Нет связи. Повторите только чтение.' : error.message;
                } finally { clearTimeout(timer); this.ozonBusy = ''; }
            },
            trackJob: function (jobUid, total, initialJob) {
                var self = this;
                this.stopJobTracking();
                var generation = this._jobGeneration;
                this._activeJobUid = jobUid;
                var inFlight = false;
                var terminal = false;
                var isCurrent = function () {
                    return self._jobGeneration === generation && self._activeJobUid === jobUid;
                };
                var finish = function (job) {
                    if (!isCurrent() || terminal) return;
                    terminal = true;
                    self.completeTrackedJob(job, generation, jobUid);
                };
                var unknownResult = function () {
                    return Object.assign({}, self.job || {}, {
                        status: 'unknown',
                        message: 'Проверьте статус этого запуска перед новой отправкой.',
                    });
                };
                var poll = function () {
                    if (!isCurrent() || terminal || inFlight || document.hidden) return;
                    inFlight = true;
                    var controller = new AbortController();
                    var requestTimer = setTimeout(function () { controller.abort(); }, 10000);
                    self._jobPollAbort = controller;
                    self._jobPollTimer = requestTimer;
                    self.$api.get(self.urls.jobStatus + jobUid, null, { signal: controller.signal }).then(function (data) {
                        if (!isCurrent() || terminal) return;
                        var job = trackedJobForUi(data, jobUid);
                        if (!job) {
                            finish(unknownResult());
                            return;
                        }
                        self.job = job;
                        if (window.shVue.isTerminalJobStatus(job.status)) finish(job);
                        else if (job.status !== 'pending' && job.status !== 'running') finish(unknownResult());
                    }).catch(function () {
                        if (isCurrent() && !terminal) finish(unknownResult());
                    }).finally(function () {
                        clearTimeout(requestTimer);
                        if (self._jobPollAbort === controller) self._jobPollAbort = null;
                        if (self._jobPollTimer === requestTimer) self._jobPollTimer = null;
                        inFlight = false;
                    });
                };
                this.job = { job_uid: jobUid, status: 'running', total: total || 0, processed: 0 };
                if (initialJob) this.job = initialJob;
                else poll();
                this._jobTimer = setInterval(poll, 2500);
            },
            completeTrackedJob: function (job, generation, jobUid) {
                if (this._jobGeneration !== generation || this._activeJobUid !== jobUid) return false;
                if (this._jobTimer) clearInterval(this._jobTimer);
                this._jobTimer = null;
                this.job = job;
                this.load();
                this.loadFacets();
                return true;
            },
            focusJobProgress: function () {
                this.$nextTick(function () {
                    var component = this.$refs.jobProgress;
                    var target = component && component.$el;
                    if (!target || typeof target.focus !== 'function') return;
                    target.focus({ preventScroll: true });
                    if (typeof target.scrollIntoView === 'function') {
                        target.scrollIntoView({ block: 'start', inline: 'nearest' });
                    }
                });
            },
            recheckJob: function () {
                var current = this.job;
                if (current && this.wbCreatePending &&
                    current.job_uid === this.wbCreatePending.job_uid) {
                    this.checkWbCreateResult(false);
                    return;
                }
                if (!current || current.status !== 'unknown' || current.checking || !current.job_uid) return;
                var self = this;
                var jobUid = current.job_uid;
                var generation = this._jobGeneration;
                var isCurrent = function () {
                    return self._jobGeneration === generation && self._activeJobUid === jobUid &&
                        self.job && self.job.status === 'unknown' && self.job.job_uid === jobUid;
                };
                this.job = Object.assign({}, current, { checking: true });
                var controller = new AbortController();
                var requestTimer = setTimeout(function () { controller.abort(); }, 10000);
                this._jobRecheckAbort = controller;
                this._jobRecheckTimer = requestTimer;
                this.$api.get(this.urls.jobStatus + jobUid, null, { signal: controller.signal }).then(function (data) {
                    if (!isCurrent()) return;
                    var job = trackedJobForUi(data, jobUid);
                    if (!job) {
                        self.job = Object.assign({}, self.job, {
                            checking: false,
                            message: 'Не удалось проверить статус. Попробуйте ещё раз.',
                        });
                        return;
                    }
                    if (window.shVue.isTerminalJobStatus(job.status)) {
                        self.completeTrackedJob(job, generation, jobUid);
                        return;
                    }
                    if (job.status === 'pending' || job.status === 'running') {
                        self.trackJob(jobUid, job.total, job);
                        return;
                    }
                    self.job = Object.assign({}, self.job, {
                        checking: false,
                        message: 'Не удалось проверить статус. Попробуйте ещё раз.',
                    });
                }).catch(function () {
                    if (!isCurrent()) return;
                    self.job = Object.assign({}, self.job, {
                        checking: false,
                        message: 'Не удалось проверить статус. Попробуйте ещё раз.',
                    });
                }).finally(function () {
                    clearTimeout(requestTimer);
                    if (self._jobRecheckAbort === controller) self._jobRecheckAbort = null;
                    if (self._jobRecheckTimer === requestTimer) self._jobRecheckTimer = null;
                });
            },
            stopJobTracking: function () {
                this._jobGeneration = (this._jobGeneration || 0) + 1;
                this._activeJobUid = null;
                if (this._jobTimer) clearInterval(this._jobTimer);
                this._jobTimer = null;
                if (this._jobPollTimer) clearTimeout(this._jobPollTimer);
                this._jobPollTimer = null;
                var pollAbort = this._jobPollAbort;
                this._jobPollAbort = null;
                if (pollAbort) pollAbort.abort();
                if (this._jobRecheckTimer) clearTimeout(this._jobRecheckTimer);
                this._jobRecheckTimer = null;
                var recheckAbort = this._jobRecheckAbort;
                this._jobRecheckAbort = null;
                if (recheckAbort) recheckAbort.abort();
            },
            dismissJob: function () {
                this.stopJobTracking();
                this.job = null;
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
    });
})();
