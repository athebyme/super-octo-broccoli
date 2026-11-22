/* Bounded same-tab workspace return context shared by Ozon repair and review. */
(function (global) {
    'use strict';

    const SESSION_KEY = 'seller-hub-ozon-review-workspace-return-v1';
    const MAX_SOURCE_URL_BYTES = 8448;
    const MAX_FRAGMENT_CHARS = 11264;
    const REVIEW_RETURN_TTL_MS = 30 * 60 * 1000;
    const MAX_IDS = 200;
    const MAX_ID = (1n << 63n) - 1n;

    function byteLength(value) {
        return new TextEncoder().encode(value).length;
    }

    function safeWorkspaceReturn(value, origin = global.location?.origin) {
        if (typeof value !== 'string' || !origin || byteLength(value) > MAX_SOURCE_URL_BYTES ||
            /[\\\u0000-\u001f\u007f]/.test(value)) return null;
        let url;
        try { url = new URL(value, origin); } catch (_) { return null; }
        if (url.origin !== origin || url.pathname !== '/my-products/beta' ||
            url.username || url.password) return null;

        let batch = '';
        if (url.search) {
            const params = url.searchParams;
            const values = params.getAll('import_batch');
            if (params.size !== 1 || values.length !== 1) return null;
            batch = values[0];
            let parts = batch.split('.');
            if (parts.length === 4 && parts[0] === '') parts = parts.slice(1);
            if (parts.length !== 3 || parts.some(part => !part || !/^[A-Za-z0-9_-]+$/.test(part)) ||
                byteLength(batch) > 8192) return null;
        }

        const fragment = url.hash ? url.hash.slice(1) : '';
        if (fragment && !/^workspace-return=[0-9a-f]{32}$/.test(fragment)) return null;
        const canonical = '/my-products/beta' +
            (batch ? '?import_batch=' + encodeURIComponent(batch) : '') +
            (fragment ? '#' + fragment : '');
        return value === canonical ? value : null;
    }

    function encodeBase64UrlUtf8(value) {
        try {
            const bytes = new TextEncoder().encode(value);
            if (bytes.length > MAX_SOURCE_URL_BYTES) return null;
            let binary = '';
            for (let offset = 0; offset < bytes.length; offset += 0x4000) {
                binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x4000));
            }
            return global.btoa(binary).replace(/=+$/g, '').replace(/\+/g, '-').replace(/\//g, '_');
        } catch (_) {
            return null;
        }
    }

    function decodeBase64UrlUtf8(encoded) {
        if (typeof encoded !== 'string' || !encoded || encoded.length > MAX_FRAGMENT_CHARS ||
            !/^[A-Za-z0-9_-]+$/.test(encoded)) return null;
        try {
            const binary = global.atob(encoded.replace(/-/g, '+').replace(/_/g, '/') +
                '='.repeat((4 - encoded.length % 4) % 4));
            const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
            if (bytes.length > MAX_SOURCE_URL_BYTES) return null;
            const canonical = global.btoa(binary).replace(/=+$/g, '').replace(/\+/g, '-').replace(/\//g, '_');
            if (canonical !== encoded) return null;
            return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
        } catch (_) {
            return null;
        }
    }

    function encodeWorkspaceReturnFragment(value, origin = global.location?.origin) {
        const safe = safeWorkspaceReturn(value, origin);
        if (!safe) return '';
        const encoded = encodeBase64UrlUtf8(safe);
        return encoded ? '#workspace-return=' + encoded : '';
    }

    function decodeWorkspaceReturnFragment(hash, origin = global.location?.origin) {
        if (typeof hash !== 'string') return null;
        const match = hash.match(/^#workspace-return=([A-Za-z0-9_-]+)$/);
        if (!match) return null;
        const decoded = decodeBase64UrlUtf8(match[1]);
        return decoded === null ? null : safeWorkspaceReturn(decoded, origin);
    }

    function repairWorkspaceReturnFromHref(href = global.location?.href) {
        let page;
        try { page = new URL(href, global.location?.origin); } catch (_) { return null; }
        const flags = page.searchParams.getAll('workspace_return');
        if (flags.length !== 1 || flags[0] !== '1') return null;
        return decodeWorkspaceReturnFragment(page.hash, page.origin);
    }

    function validDraftIds(value) {
        return Array.isArray(value) && value.length > 0 && value.length <= MAX_IDS &&
            value.every(id => Number.isSafeInteger(id) && id > 0) &&
            new Set(value).size === value.length;
    }

    function encodeCompactDraftIds(ids) {
        if (!validDraftIds(ids)) return null;
        const bytes = [];
        for (const value of ids) {
            let id = BigInt(value);
            const offset = bytes.length;
            for (let index = 7; index >= 0; index--) {
                bytes[offset + index] = Number(id & 255n);
                id >>= 8n;
            }
        }
        let binary = '';
        for (const byte of bytes) binary += String.fromCharCode(byte);
        return global.btoa(binary).replace(/=+$/g, '').replace(/\+/g, '-').replace(/\//g, '_');
    }

    function validWorkspaceOwner(value) {
        if (typeof value !== 'string' || value.length > 39) return false;
        const match = value.match(/^([1-9][0-9]{0,18}):([1-9][0-9]{0,18})$/);
        if (!match) return false;
        try {
            const userId = BigInt(match[1]), sellerId = BigInt(match[2]);
            return userId <= MAX_ID && sellerId <= MAX_ID;
        } catch (_) {
            return false;
        }
    }

    function reviewWorkspaceScope(review) {
        if (!review || typeof review !== 'object' || Array.isArray(review) ||
            !Number.isSafeInteger(review.account_id) || review.account_id <= 0 ||
            !validDraftIds(review.draft_ids) || !validWorkspaceOwner(review.workspace_owner)) return null;
        const selection = encodeCompactDraftIds(review.draft_ids);
        if (!selection || review.workspace_selection !== selection) return null;
        return {
            owner: review.workspace_owner,
            accountId: review.account_id,
            workspaceSelection: selection,
            draftIds: review.draft_ids.slice(),
        };
    }

    function sameIds(left, right) {
        return Array.isArray(left) && Array.isArray(right) && left.length === right.length &&
            left.every((id, index) => id === right[index]);
    }

    function reviewReturnStorage(options) {
        if (options && Object.prototype.hasOwnProperty.call(options, 'storage')) return options.storage;
        try { return global.sessionStorage; } catch (_) { return null; }
    }

    function nowValue(options) {
        return options && typeof options.now === 'function' ? options.now() : Date.now();
    }

    function removeStored(storage) {
        try { storage?.removeItem(SESSION_KEY); } catch (_) {}
    }

    function storeReviewWorkspaceReturn(scope, value, options) {
        const storage = reviewReturnStorage(options);
        const href = safeWorkspaceReturn(value, options?.origin || global.location?.origin);
        const now = nowValue(options);
        if (!scope || !validWorkspaceOwner(scope.owner) ||
            !Number.isSafeInteger(scope.accountId) || scope.accountId <= 0 ||
            !validDraftIds(scope.draftIds) ||
            scope.workspaceSelection !== encodeCompactDraftIds(scope.draftIds) ||
            !href || !Number.isSafeInteger(now) || now < 0 ||
            !Number.isSafeInteger(now + REVIEW_RETURN_TTL_MS) ||
            !storage || typeof storage.setItem !== 'function') return null;
        const record = {
            v: 1,
            owner: scope.owner,
            account_id: scope.accountId,
            workspace_selection: scope.workspaceSelection,
            draft_ids: scope.draftIds.slice(),
            href,
            expires_at: now + REVIEW_RETURN_TTL_MS,
        };
        try {
            storage.setItem(SESSION_KEY, JSON.stringify(record));
            return href;
        } catch (_) {
            return null;
        }
    }

    function readReviewWorkspaceReturn(scope, options) {
        const storage = reviewReturnStorage(options);
        if (!storage || typeof storage.getItem !== 'function') return null;
        let raw = null, record = null;
        try {
            raw = storage.getItem(SESSION_KEY);
            record = JSON.parse(raw || 'null');
        } catch (_) { record = null; }
        const now = nowValue(options);
        const valid = record && typeof record === 'object' && !Array.isArray(record) &&
            Object.keys(record).sort().join(',') ===
                'account_id,draft_ids,expires_at,href,owner,v,workspace_selection' &&
            record.v === 1 && scope && record.owner === scope.owner &&
            record.account_id === scope.accountId &&
            record.workspace_selection === scope.workspaceSelection &&
            sameIds(record.draft_ids, scope.draftIds) &&
            Number.isSafeInteger(record.expires_at) && Number.isSafeInteger(now) && now >= 0 &&
            record.expires_at > now && record.expires_at <= now + REVIEW_RETURN_TTL_MS &&
            safeWorkspaceReturn(record.href, options?.origin || global.location?.origin);
        if (!valid) {
            if (raw !== null) removeStored(storage);
            return null;
        }
        return record.href;
    }

    function resolveReviewWorkspaceReturn(review, href = global.location?.href, options) {
        const scope = reviewWorkspaceScope(review);
        if (!scope) return null;
        let page;
        try { page = new URL(href, options?.origin || global.location?.origin); } catch (_) { return null; }
        if (page.hash) {
            const decoded = decodeWorkspaceReturnFragment(page.hash, page.origin);
            if (!decoded) return null;
            storeReviewWorkspaceReturn(scope, decoded, {
                ...options,
                origin: page.origin,
            });
            return decoded;
        }
        return readReviewWorkspaceReturn(scope, options);
    }

    global.OzonWorkspaceReturn = {
        SESSION_KEY,
        REVIEW_RETURN_TTL_MS,
        safeWorkspaceReturn,
        encodeWorkspaceReturnFragment,
        decodeWorkspaceReturnFragment,
        repairWorkspaceReturnFromHref,
        encodeCompactDraftIds,
        reviewWorkspaceScope,
        storeReviewWorkspaceReturn,
        readReviewWorkspaceReturn,
        resolveReviewWorkspaceReturn,
    };
})(window);
