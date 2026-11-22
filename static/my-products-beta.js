/* Мои товары · beta — экран массовых операций.
   Правило страницы: выбрал строки → видишь, что произойдёт → подтвердил →
   следишь за прогрессом → получаешь честный итог. */
(function () {
    'use strict';

    if (typeof window.shVue === 'undefined') return;
    var boot = window.shVue.bootstrapData('mp-bootstrap');
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
            new Set(value.imported_product_ids).size === value.imported_product_ids.length;
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
    var SUPPLIER_REFRESH_PREVIEW_TIMEOUT_MS = 12000;
    var WORKSPACE_RETURN_SCOPE_KEYS = [
        'activeTab', 'search', 'supplier', 'has_photos', 'stock', 'sort', 'visibleCount',
    ];
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

    function storePriceWorkspaceReturn(app, priceTarget, selectionIds, scope) {
        if (!scope || !Array.isArray(selectionIds)) return null;
        var helpers = window.SellerHubCommonContent || {};
        if (typeof helpers.storeWorkspaceReturn !== 'function') return null;
        try {
            return helpers.storeWorkspaceReturn({
                owner: boot.workspaceOwner,
                priceTarget: priceTarget,
                selectionIds: selectionIds.slice(),
                scope: scope,
                batch: app.importBatchMode ? app.importBatch : null,
            });
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

    function exactOzonPriceAccountChoices(items, selectionIds, accounts) {
        if (!Array.isArray(selectionIds) || !selectionIds.length ||
            selectionIds.length > MAX_OZON_PRICE_HANDOFF) {
            return {
                choices: [],
                reason: Array.isArray(selectionIds) && selectionIds.length > MAX_OZON_PRICE_HANDOFF
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

        var summary = 'Обновление из поставщика. В запросе товаров: ' + data.requested + '.';

        var title = data.errors
            ? 'Обновление завершено с ошибками'
            : data.updated === 0 && data.skipped > 0
                ? 'Нет карточек для обновления'
                : 'Обновление из поставщика завершено';
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
            summary: 'Обновление из поставщика. В запросе товаров: '
                + requested + '. ' + message,
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

    var TABS = [
        { key: '', label: 'Все', countKey: 'all' },
        { key: 'pending', label: 'В работе', countKey: 'pending' },
        { key: 'validated', label: 'Готовы к WB', countKey: 'validated' },
        { key: 'imported', label: 'На WB', countKey: 'imported' },
        { key: 'imported_pending_sync', label: 'Ждут артикул WB', countKey: 'imported_pending_sync' },
        { key: 'failed', label: 'Ошибки', countKey: 'failed' },
        { key: 'updates', label: 'Обновления поставщика', countKey: 'supplier_updates' },
    ];

    function validWorkspaceReturnScope(scope) {
        if (!scope || typeof scope !== 'object' || Array.isArray(scope)) return false;
        var keys = Object.keys(scope).sort();
        var expectedKeys = WORKSPACE_RETURN_SCOPE_KEYS.slice().sort();
        if (keys.length !== expectedKeys.length ||
            !keys.every(function (key, index) { return key === expectedKeys[index]; })) return false;
        var supplierIsActive = scope.supplier === '' || (
            Number.isSafeInteger(scope.supplier) && scope.supplier > 0 &&
            Array.isArray(boot.suppliers) && boot.suppliers.some(function (supplier) {
                return supplier && supplier.id === scope.supplier;
            })
        );
        return TABS.some(function (tab) { return tab.key === scope.activeTab; }) &&
            typeof scope.search === 'string' && scope.search.length <= 200 &&
            scope.search.trim() === scope.search && supplierIsActive &&
            ['', 'yes', 'no'].indexOf(scope.has_photos) !== -1 &&
            ['', 'in_stock', 'out_of_stock'].indexOf(scope.stock) !== -1 &&
            ['', 'oldest', 'price_asc', 'price_desc', 'title'].indexOf(scope.sort) !== -1 &&
            Number.isSafeInteger(scope.visibleCount) &&
            scope.visibleCount >= 1 && scope.visibleCount <= 200;
    }

    function defaultWorkspaceReturnScope(scope) {
        return scope.activeTab === '' && scope.search === '' && scope.supplier === '' &&
            scope.has_photos === '' && scope.stock === '' && scope.sort === '';
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

    window.shVue.mount('#my-products-app', {
        data: function () {
            var savedOzon = readOzonPending();
            return {
                wbConnected: !!boot.wbConnected,
                ozonEnabled: !!boot.ozonEnabled,
                ozonAccounts: boot.ozonAccounts || [],
                suppliers: boot.suppliers || [],
                urls: boot.urls || {},
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
                },
                selected: [],
                ozonIds: [],
                ozonAccountId: '',
                ozonConfirmed: false,
                ozonBusy: '',
                ozonError: '',
                ozonReturnFocus: null,
                ozonPending: validOzonPending(savedOzon, boot.ozonAccounts || []) ? savedOzon : null,
                ozonStorageProblem: hasOzonPending() && !validOzonPending(savedOzon, boot.ozonAccounts || []),
                ozonRetrySameKey: false,
                ozonSessionEnded: false,
                ozonPriceError: '',
                ozonPriceAccountId: '',
                ozonPriceHandoffReview: null,
                ozonPriceDialogError: '',
                imgFailed: {},
                tabs: TABS,

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
                var chosen = this.selected;
                return this.items.filter(function (item) {
                    return chosen.indexOf(item.id) !== -1;
                });
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
            /* Основные операции показываются вместе; редкие — в «Ещё». */
            actionGroups: function () {
                var chosen = this.selectedItems;
                var createRows = chosen.filter(function (item) {
                    return !item.wb && ['pending', 'failed', 'validated'].indexOf(item.import_status) !== -1;
                });
                var wbIds = exactWbProductIds(chosen);
                var withUpdates = chosen.filter(function (i) { return i.has_supplier_update; }).length;
                var underLimit = chosen.length <= MAX_BULK_SELECTION;
                return [{
                    title: 'Основные действия',
                    items: [
                        {
                            key: 'push_wb',
                            label: 'Создать карточки WB',
                            hint: !this.wbConnected
                                ? 'подключите кабинет Wildberries'
                                : !underLimit
                                ? 'за один запуск можно выбрать не больше 200 товаров'
                                : createRows.length
                                    ? 'для ' + createRows.length + ' товаров, ещё не связанных с WB'
                                    : 'выберите товары, которые ещё не связаны с WB',
                            disabled: !this.wbConnected || !underLimit || !createRows.length,
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
                                    : 'обновит только ваши данные в Seller Hub, карточки площадок не меняются',
                            disabled: !underLimit,
                        },
                        {
                            key: 'common_content',
                            label: 'Изменить общий контент',
                            hint: chosen.length > 50
                                ? 'за один раз можно открыть не больше 50 товаров'
                                : 'изменит данные в Seller Hub; карточки площадок не обновятся',
                            disabled: !chosen.length || chosen.length > 50,
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
                            hint: !chosen.length
                                ? 'выберите товары'
                                : chosen.length > MAX_OZON_PRICE_HANDOFF
                                    ? 'за одно сравнение можно выбрать не больше 100 товаров'
                                    : 'откроется сравнение для одного магазина, где есть все выбранные карточки',
                            disabled: !chosen.length || chosen.length > MAX_OZON_PRICE_HANDOFF,
                        } : null,
                    ].filter(Boolean),
                }];
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
                            ? published + ' ' + this.$fmt.plural(published, 'карточка WB останется', 'карточки WB останутся', 'карточек WB останутся') + ' на площадке'
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
                if (this.filters.search || this.filters.supplier
                    || this.filters.has_photos || this.filters.stock) {
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
                    this.selected = [];
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
                    if (this._selectAllAbort &&
                        !sameOrderedIds(next, this._selectAllSelectionSnapshot)) {
                        this.cancelFilteredSelect();
                    }
                    var review = this.supplierRefreshReview;
                    if (review && review.phase !== 'applying' &&
                        !sameOrderedIds(next, review.selectionIds)) {
                        this.cancelSupplierRefreshReview({ restoreFocus: false });
                    }
                },
            },
            ozonError: function (value) {
                if (value) this.$nextTick(function () { this.$refs.ozonError?.focus(); });
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
            if (!this.importBatchMode && !this.supplierRefreshBatchMode) this.loadFacets();
        },
        beforeUnmount: function () {
            this.stopJobTracking();
            if (this._abort) this._abort.abort();
            if (this._loadMoreAbort) this._loadMoreAbort.abort();
            this.cancelSupplierRefreshRestore();
            this._supplierRefreshUnmounted = true;
            this.cancelSupplierRefreshReview({ restoreFocus: false, force: true });
            this.cancelFilteredSelect();
        },

        methods: {
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
                this.filters = { search: '', supplier: '', has_photos: '', stock: '', sort: '' };
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
                if (this.activeTab === 'updates') params.updates = '1';
                else if (this.activeTab) params.status = this.activeTab;
                return params;
            },
            consumeWorkspaceReturn: function (nonce) {
                this.workspaceReturnPending = false;
                if (typeof workspaceHelpers.consumeWorkspaceReturn === 'function') {
                    try { workspaceHelpers.consumeWorkspaceReturn(nonce); } catch (_) {}
                }
            },
            workspaceReturnChanged: function () {
                this.selected = [];
                this.workspaceReturnNote = workspaceChangedNote;
                if (workspaceReturnCapsule) this.consumeWorkspaceReturn(workspaceReturnCapsule.nonce);
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
                        (this.searchInput || '').trim().slice(0, 200) === scope.search;
            },
            buildWorkspaceReturnScope: function () {
                if (this.loading || this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) return null;
                var visibleCount = Math.min(this.items.length, MAX_BULK_SELECTION);
                if (!visibleCount || !orderedProductIds(this.items.slice(0, visibleCount))) return null;
                var scope = {
                    activeTab: this.activeTab,
                    search: this.filters.search,
                    supplier: this.filters.supplier,
                    has_photos: this.filters.has_photos,
                    stock: this.filters.stock,
                    sort: this.filters.sort,
                    visibleCount: visibleCount,
                };
                return validWorkspaceReturnScope(scope) &&
                    (!this.importBatchMode || defaultWorkspaceReturnScope(scope)) ? scope : null;
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
                    this.error = 'Не удалось открыть общий контент. Обновите страницу и повторите действие.';
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
                    this.error = 'Не удалось открыть общий контент. Обновите страницу и повторите действие.';
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
                this.cancelSupplierRefreshReview({ restoreFocus: false });
                var workspaceReturn = this.workspaceReturnPending ? workspaceReturnCapsule : null;
                if (workspaceReturn && !this.workspaceReturnScopeMatches(workspaceReturn.scope)) {
                    this.workspaceReturnChanged();
                    workspaceReturn = null;
                }
                if (this._abort) this._abort.abort();
                if (this._loadMoreAbort) this._loadMoreAbort.abort();
                this._loadMoreAbort = null;
                this.loadingMore = false;
                this.cancelFilteredSelect();
                this.selected = [];
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
                                if (batchIds && workspaceReturn.selectionIds.every(function (id) {
                                    return batchIds.indexOf(id) !== -1;
                                })) {
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
                            if (workspaceReturn.selectionIds.every(function (id) {
                                return returnedPage.ids.indexOf(id) !== -1;
                            })) {
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
                            if (self.supplierRefreshSelectPending) {
                                self.selected = verifiedBatchIds.slice();
                            }
                            self.supplierRefreshSelectPending = false;
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
                if (this.importBatchMode || this.supplierRefreshBatchMode) return;
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
                if (this.importBatchMode || this.supplierRefreshBatchMode) return;
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
                        self.selected = fullIds.slice();
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
            setTab: function (key) {
                if (this.importBatchMode || this.supplierRefreshBatchMode) return;
                if (this.activeTab === key) return;
                this.activeTab = key;
                this.selected = [];
                this.load();
            },
            resetFilters: function () {
                if (this.importBatchMode || this.supplierRefreshBatchMode) return;
                this.searchInput = '';
                this.filters.search = '';
                this.filters.supplier = '';
                this.filters.has_photos = '';
                this.filters.stock = '';
            },
            toggleAll: function () {
                this.selected = this.allSelected
                    ? []
                    : this.items.map(function (item) { return item.id; });
            },
            resolveOzonPriceAccountChoices: function (items, selectionIds) {
                return exactOzonPriceAccountChoices(items, selectionIds, this.ozonAccounts);
            },
            setOzonPriceHandoffError: function (message) {
                this.ozonPriceError = message;
                this.$nextTick(function () {
                    this.$refs.ozonPriceError?.focus();
                });
            },
            openOzonPriceHandoff: function (items, selectionIds) {
                this.ozonPriceError = '';
                this.ozonPriceDialogError = '';
                if (!sameIds(this.selected, selectionIds)) {
                    this.setOzonPriceHandoffError(
                        'Выбор товаров изменился. Отметьте товары заново и повторите действие.'
                    );
                    return;
                }
                var result = this.resolveOzonPriceAccountChoices(items, selectionIds);
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
                this._ozonPriceTrigger = document.activeElement;
                this.ozonPriceAccountId = '';
                this.$nextTick(function () {
                    this.$refs.ozonPriceAccountDialog.showModal();
                    this.$refs.ozonPriceAccount.focus();
                });
            },
            closeOzonPriceHandoff: function () {
                var dialog = this.$refs.ozonPriceAccountDialog;
                if (dialog && dialog.open) dialog.close();
                this.ozonPriceHandoffReview = null;
                this.ozonPriceAccountId = '';
                this.ozonPriceDialogError = '';
                this._ozonPriceTrigger?.focus();
            },
            continueOzonPriceHandoff: function () {
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
                if (!review || !sameIds(this.selected, review.imported_product_ids)) {
                    this.closeOzonPriceHandoff();
                    this.setOzonPriceHandoffError(
                        'Выбор товаров изменился после проверки. Отметьте их заново перед сравнением.'
                    );
                    return false;
                }
                var selectedItems = this.selectedItems;
                var latest = this.resolveOzonPriceAccountChoices(
                    selectedItems,
                    this.selected,
                );
                var freshChoice = latest.choices.find(function (choice) {
                    return choice.account_id === accountId;
                });
                var reviewedChoice = review.choices.find(function (choice) {
                    return choice.account_id === accountId;
                });
                if (!freshChoice || !reviewedChoice ||
                    !sameIds(freshChoice.listing_ids, reviewedChoice.listing_ids)) {
                    this.closeOzonPriceHandoff();
                    this.setOzonPriceHandoffError(
                        'Связанные карточки изменились после проверки. Обновите список и повторите действие.'
                    );
                    return false;
                }
                var returnScope = this.buildWorkspaceReturnScope();
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
            startAction: function (action) {
                if (this.loading || this.itemsScope !== this.feedScopeKey() ||
                    (this.searchInput || '').trim().slice(0, 200) !== this.filters.search) {
                    this.error = 'Список обновляется. Дождитесь загрузки и выберите товары снова.';
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
                            'Изменятся только данные в Seller Hub; карточки на площадках не меняются.',
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
                    this.openOzonPreparation(selectionIds);
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
                if (action.key === 'wb_prices') {
                    var priceIds = exactWbProductIds(chosen);
                    if (!priceIds || priceIds.length > MAX_BULK_SELECTION || !this.wbConnected) {
                        this.error = 'Для изменения цены выберите до 200 карточек WB и проверьте подключение кабинета.';
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
                if (action.key === 'ozon_prices') {
                    if (selectionIds.length > MAX_OZON_PRICE_HANDOFF) {
                        this.setOzonPriceHandoffError(
                            'За одно сравнение Ozon можно выбрать не больше 100 товаров.'
                        );
                        return;
                    }
                    this.openOzonPriceHandoff(chosen, selectionIds);
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

                var request;
                if (key === 'push_wb') {
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
                this.ozonAccountId = this.ozonAccounts.length === 1
                    ? String(this.ozonAccounts[0].id) : '';
                this.ozonConfirmed = false;
                this.ozonError = '';
                this.ozonReturnFocus = returnFocus || null;
                this.$nextTick(function () {
                    this.$refs.ozonDialog.showModal();
                    this.$refs.ozonAccount.focus();
                });
                return true;
            },
            closeOzonDialog: function () {
                if (this.ozonBusy) return;
                this.$refs.ozonDialog.close();
                this.ozonConfirmed = false;
                if (this.ozonReturnFocus && this.ozonReturnFocus.isConnected &&
                    typeof this.ozonReturnFocus.focus === 'function') {
                    this.ozonReturnFocus.focus();
                }
                this.ozonReturnFocus = null;
            },
            clearOzonStorageProblem: function () {
                if (!this.ozonStorageProblem || this.ozonBusy) return;
                clearOzonPending(); this.ozonStorageProblem = false;
                this.ozonError = 'Повреждённая запись снята после проверки истории. Выберите товары заново перед подготовкой.';
            },
            prepareOzon: async function () {
                var accountId = Number(this.ozonAccountId);
                if (this.ozonBusy || this.ozonPending || this.ozonStorageProblem || this.ozonSessionEnded ||
                    !this.ozonConfirmed || !Number.isSafeInteger(accountId) ||
                    !this.ozonAccounts.some(function (row) { return row.id === accountId; }) ||
                    !this.ozonIds.length || this.ozonIds.length > 200) return;
                var csrf = document.querySelector('meta[name="csrf-token"]')?.content;
                if (!csrf) { this.ozonError = 'Защита формы недоступна. Обновите страницу перед подготовкой.'; return; }
                var pending = {account_id:accountId, imported_product_ids:this.ozonIds.slice(),
                    request_key:ozonRequestKey()};
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
                    bindOzonPreparationWorkspaceReturn(pending, data.run.job_uid);
                    clearOzonPending(); this.ozonPending = null;
                    window.location.assign(this.urls.ozonUploads + data.run.job_uid);
                } catch (error) {
                    if (error.status === 401 || error.status === 403) this.ozonSessionEnded = true;
                    this.ozonError = error.name === 'AbortError'
                        ? 'Ответ не пришёл вовремя. Повторная подготовка заблокирована; проверьте результат.'
                        : error instanceof TypeError
                            ? 'Связь прервалась. Повторная подготовка заблокирована; проверьте результат.'
                            : error.message;
                } finally {
                    clearTimeout(timer); this.ozonBusy = '';
                    this.$refs.ozonDialog.close();
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
                    bindOzonPreparationWorkspaceReturn(this.ozonPending, data.run.job_uid);
                    clearOzonPending(); this.ozonPending = null;
                    window.location.assign(this.urls.ozonUploads + data.run.job_uid);
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
