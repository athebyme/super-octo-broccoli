#!/usr/bin/env node
'use strict';

// Execute the production editor handlers against a deliberately small DOM.
// The DOM models removal, hidden/inert ancestry, disabled controls, and native
// focus failure so a replacement handler cannot pass by testing a duplicate
// focus algorithm or by focusing an element that a browser would reject.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const productionPath = path.resolve(process.argv[2] || path.join(__dirname, '../../static/common-product-content.js'));

function parseSelector(selector) {
    const tag = selector.match(/^[a-zA-Z][\w-]*/);
    const attrs = [...selector.matchAll(/\[([^\]=]+)(?:=["']?([^\]"']+)["']?)?\]/g)];
    return (element) => (!tag || element.tagName.toLowerCase() === tag[0].toLowerCase())
        && attrs.every((match) => {
            const key = match[1].trim();
            const actual = key.startsWith('data-')
                ? element.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())]
                : element.getAttribute(key);
            return actual !== undefined && actual !== null
                && (match[2] === undefined || String(actual) === match[2]);
        });
}

class MiniElement {
    constructor(document, tagName) {
        this.ownerDocument = document;
        this.tagName = String(tagName || 'div').toUpperCase();
        this.parentNode = null;
        this.children = [];
        this.dataset = {};
        this.attributes = new Map();
        this.listeners = new Map();
        this._text = '';
        this._hidden = false;
        this._inert = false;
        this.disabled = false;
        this.checked = false;
        this.value = '';
        this.type = '';
        this.tabIndex = -1;
    }

    get hidden() { return this._hidden; }
    set hidden(value) {
        this._hidden = !!value;
        this.ownerDocument.invalidateFocus(this);
    }

    get inert() { return this._inert; }
    set inert(value) {
        this._inert = !!value;
        this.ownerDocument.invalidateFocus(this);
    }

    get textContent() {
        return this._text + this.children.map((child) => child.textContent).join('');
    }

    get lastChild() { return this.children[this.children.length - 1] || null; }

    set textContent(value) {
        this.replaceChildren();
        this._text = String(value == null ? '' : value);
    }

    get isConnected() {
        let node = this;
        while (node) {
            if (node === this.ownerDocument.body) return true;
            node = node.parentNode;
        }
        return false;
    }

    appendChild(child) {
        if (child.parentNode) child.parentNode.removeChild(child);
        child.parentNode = this;
        this.children.push(child);
        this._text = '';
        return child;
    }

    append(...children) { children.forEach((child) => this.appendChild(child)); }

    removeChild(child) {
        const index = this.children.indexOf(child);
        if (index < 0) return child;
        this.ownerDocument.invalidateFocus(child, true);
        this.children.splice(index, 1);
        child.parentNode = null;
        return child;
    }

    replaceChildren(...children) {
        for (const child of [...this.children]) this.removeChild(child);
        this._text = '';
        children.forEach((child) => this.appendChild(child));
    }

    setAttribute(name, value) {
        this.attributes.set(String(name), String(value));
        if (name === 'hidden') this.hidden = true;
        if (name === 'inert') this.inert = true;
    }

    getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }

    addEventListener(type, callback) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(callback);
    }

    dispatchEvent(event) {
        if (!event.target) event.target = this;
        if (!event.preventDefault) event.preventDefault = function () { this.defaultPrevented = true; };
        if (!event.stopPropagation) event.stopPropagation = function () { this._stopped = true; };
        let current = this;
        while (current) {
            event.currentTarget = current;
            (current.listeners.get(event.type) || []).forEach((callback) => callback(event));
            if (event._stopped) break;
            current = current.parentNode;
        }
        return !event.defaultPrevented;
    }

    matches(selector) { return parseSelector(selector)(this); }

    closest(selector) {
        let current = this;
        while (current) {
            if (current.matches(selector)) return current;
            current = current.parentNode;
        }
        return null;
    }

    contains(other) {
        let current = other;
        while (current) {
            if (current === this) return true;
            current = current.parentNode;
        }
        return false;
    }

    querySelectorAll(selector) {
        const matches = parseSelector(selector);
        const found = [];
        const visited = new Set();
        const visit = (parent) => parent.children.forEach((child) => {
            if (visited.has(child)) throw new Error('synthetic DOM contains a child cycle');
            visited.add(child);
            if (matches(child)) found.push(child);
            visit(child);
        });
        visit(this);
        return found;
    }

    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

    focus() {
        if (!this.isConnected || this.disabled || this.hidden || this.inert) return;
        let current = this.parentNode;
        while (current) {
            if (current.hidden || current.inert) return;
            current = current.parentNode;
        }
        this.ownerDocument.activeElement = this;
    }
}

class MiniDocument {
    constructor() {
        this.activeElement = null;
        this.body = new MiniElement(this, 'body');
        this.activeElement = this.body;
        this.nodes = new Map();
    }

    createElement(tagName) { return new MiniElement(this, tagName); }
    getElementById(id) { return this.nodes.get(id) || null; }

    invalidateFocus(container, removed = false) {
        const active = this.activeElement;
        if (!active || !container || !container.contains(active)) return;
        if (removed || container.hidden || container.inert) this.activeElement = this.body;
    }

    mount(id, parent, tagName = 'div') {
        const item = this.createElement(tagName);
        item.id = id;
        this.nodes.set(id, item);
        parent.appendChild(item);
        return item;
    }
}

function fixtureProduct(id, title) {
    const photos = ['photo-a', 'photo-b'];
    const chars = [{ name: 'Material', value: 'cotton' }, { name: 'Finish', value: 'matte' }];
    return {
        product_id: id,
        title,
        external_id: 'SKU-' + id,
        content_edit_version: 1,
        source: { type: 'manual', revision: 1, field_origins: {} },
        fields: {
            title: { effective: title, inherited: 'Source ' + title, origin: 'seller_override', inherited_origin: 'source', is_overridden: true },
            description: { effective: 'Description ' + id, inherited: 'Source description ' + id, origin: 'source', inherited_origin: 'source', is_overridden: false },
            photos: { effective: photos, inherited: photos, origin: 'seller_override', inherited_origin: 'source', is_overridden: true },
            characteristics: { effective: chars, inherited: chars, origin: 'seller_override', inherited_origin: 'source', is_overridden: true },
        },
        photo_options: photos.map((url, index) => ({
            url, preview_url: null, source: 'catalog', available_for_selection: true, index,
        })),
        recipients: [],
    };
}

function response(payload, status = 200) {
    return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        json: () => Promise.resolve(payload),
    });
}

function makeApp(options = {}) {
    const document = new MiniDocument();
    const root = document.mount('common-content-editor', document.body);
    const bootstrapNode = document.mount('common-content-bootstrap', root, 'script');
    const selection = document.mount('selection', root);
    const listNode = document.mount('common-content-product-list', selection, 'ul');
    const workspace = document.mount('workspace', root);
    const errorNode = document.mount('common-content-error', workspace);
    errorNode.hidden = true;
    const statusNode = document.mount('common-content-status', workspace);
    statusNode.hidden = true;
    const fieldsNode = document.mount('common-content-fields', workspace);
    const previewNode = document.mount('common-content-preview', workspace);
    previewNode.hidden = true;
    const products = [fixtureProduct(11, 'Product One'), fixtureProduct(22, 'Product Two')];
    const bootstrap = {
        products,
        selectedProductIds: [11, 22],
        csrfToken: 'synthetic-only',
        previewUrl: '/api/common-content/preview',
        applyUrl: '/api/common-content/apply',
        readUrlBase: '/api/my-products/',
        catalogUrl: '/my-products',
    };
    bootstrapNode.textContent = JSON.stringify(bootstrap);

    const calls = { preview: 0, apply: [], get: 0 };
    const fakeFetch = (url, init = {}) => {
        const method = (init.method || 'GET').toUpperCase();
        if (method === 'GET') {
            calls.get += 1;
            if (options.getFailure || (options.readbackFailure && calls.apply.length)) {
                return response({ success: false, error: 'synthetic read unavailable' }, 503);
            }
            const id = Number(String(url).match(/my-products\/(\d+)/)?.[1]);
            const product = products.find((row) => row.product_id === id);
            return response({ success: !!product, product });
        }
        const body = JSON.parse(init.body || '{}');
        if (url === bootstrap.previewUrl) {
            calls.preview += 1;
            return response({
                success: true,
                save_effect: 'common_only',
                preview: {
                    preview_token: 'synthetic-preview-' + calls.preview,
                    items: [{ product_id: 11, fields: [], recipients: [] }],
                },
            });
        }
        if (url === bootstrap.applyUrl) {
            calls.apply.push(body.preview_token);
            return response({ success: true, save_effect: 'common_only', applied: [{ product_id: 11, content_edit_version: 2 }] });
        }
        return response({ success: false, error: 'unrecognized synthetic request' }, 404);
    };
    const windowListeners = new Map();
    const window = {
        SellerHubCommonContent: {},
        confirm: () => true,
        addEventListener(type, callback) {
            if (!windowListeners.has(type)) windowListeners.set(type, []);
            windowListeners.get(type).push(callback);
        },
        setTimeout,
        clearTimeout,
    };
    const module = { exports: {} };
    const context = {
        module,
        exports: module.exports,
        document,
        window,
        fetch: fakeFetch,
        AbortController,
        URLSearchParams,
        CSS: { escape: (value) => String(value) },
        console,
        setTimeout,
        clearTimeout,
    };
    vm.runInNewContext(fs.readFileSync(productionPath, 'utf8'), context, { filename: productionPath });
    return { document, root, listNode, fieldsNode, previewNode, errorNode, statusNode, products, calls };
}

function find(node, action, predicate = () => true) {
    const item = node.querySelectorAll('[data-action]').find((candidate) =>
        candidate.dataset.action === action && predicate(candidate));
    assert.ok(item, 'expected action ' + action + ' in rendered production DOM');
    return item;
}

function dispatchClick(item) {
    if (!item.disabled) item.focus();
    item.dispatchEvent({ type: 'click', target: item });
}
function dispatchInput(item, value) {
    item.value = value;
    item.dispatchEvent({ type: 'input', target: item });
}

async function drain() {
    for (let index = 0; index < 8; index += 1) {
        await new Promise((resolve) => setImmediate(resolve));
    }
}

function assertFocused(app, element, label) {
    assert.equal(app.document.activeElement === element, true, label + ' should own native activeElement');
    assert.ok(element.isConnected, label + ' should remain connected');
    assert.ok(!element.disabled, label + ' should be enabled');
    let parent = element;
    while (parent) {
        assert.ok(!parent.hidden && !parent.inert, label + ' should not be hidden or inert');
        parent = parent.parentNode;
    }
}

async function photoBoundaryFocus() {
    const app = makeApp();
    const second = 'photo-b';
    dispatchClick(find(app.fieldsNode, 'move-photo', (item) => item.dataset.photoUrl === second && item.dataset.direction === '-1'));
    const secondDown = find(app.fieldsNode, 'move-photo', (item) => item.dataset.photoUrl === second && item.dataset.direction === '1');
    assertFocused(app, secondDown, 'photo moved to first position must focus its enabled down control');
    assert.ok(find(app.fieldsNode, 'move-photo', (item) => item.dataset.photoUrl === second && item.dataset.direction === '-1').disabled);

    dispatchClick(secondDown);
    const secondUp = find(app.fieldsNode, 'move-photo', (item) => item.dataset.photoUrl === second && item.dataset.direction === '-1');
    assertFocused(app, secondUp, 'photo moved to last position must focus its enabled up control');
    assert.ok(find(app.fieldsNode, 'move-photo', (item) => item.dataset.photoUrl === second && item.dataset.direction === '1').disabled);
}

async function previewReturnDoesNotReuseToken() {
    const app = makeApp();
    dispatchInput(app.fieldsNode.querySelector('[data-field-input="title"]'), 'Changed title');
    dispatchClick(find(app.fieldsNode, 'preview'));
    await drain();
    assert.equal(app.calls.preview, 1);

    const back = find(app.previewNode, 'back-to-fields');
    back.focus();
    dispatchClick(back);
    const freshTrigger = find(app.fieldsNode, 'preview');
    assertFocused(app, freshTrigger, 'return from preview');

    dispatchClick(freshTrigger);
    await drain();
    assert.equal(app.calls.preview, 2, 'return flow must request a fresh preview');
    const acknowledgement = app.previewNode.querySelector('[data-action="acknowledge-preview"]');
    acknowledgement.checked = true;
    acknowledgement.dispatchEvent({ type: 'change', target: acknowledgement });
    dispatchClick(find(app.previewNode, 'apply'));
    await drain();
    assert.deepEqual(app.calls.apply, ['synthetic-preview-2'], 'apply must use the fresh post-return preview token');
    assertFocused(app, find(app.fieldsNode, 'toggle-mode'), 'successful apply');

    const readbackFailureApp = makeApp({ readbackFailure: true });
    dispatchInput(readbackFailureApp.fieldsNode.querySelector('[data-field-input="title"]'), 'Saved before readback issue');
    dispatchClick(find(readbackFailureApp.fieldsNode, 'preview'));
    await drain();
    const failedReadbackAck = readbackFailureApp.previewNode.querySelector('[data-action="acknowledge-preview"]');
    failedReadbackAck.checked = true;
    failedReadbackAck.dispatchEvent({ type: 'change', target: failedReadbackAck });
    dispatchClick(find(readbackFailureApp.previewNode, 'apply'));
    await drain();
    assert.ok(!readbackFailureApp.statusNode.hidden);
    assert.match(readbackFailureApp.statusNode.textContent, /обновить отображение не удалось/);
    assertFocused(readbackFailureApp, find(readbackFailureApp.fieldsNode, 'toggle-mode'), 'saved state after readback failure');

    const externalApp = makeApp();
    dispatchInput(externalApp.fieldsNode.querySelector('[data-field-input="title"]'), 'Another changed title');
    dispatchClick(find(externalApp.fieldsNode, 'preview'));
    await drain();
    const externalAck = externalApp.previewNode.querySelector('[data-action="acknowledge-preview"]');
    externalAck.checked = true;
    externalAck.dispatchEvent({ type: 'change', target: externalAck });
    const externalControl = externalApp.document.mount('outside-editor-control', externalApp.document.body, 'button');
    dispatchClick(find(externalApp.previewNode, 'apply'));
    externalControl.focus();
    await drain();
    assert.equal(externalApp.document.activeElement === externalControl, true, 'apply completion must not steal focus from outside the editor');
}

async function localReplacementFocusInventory() {
    let app = makeApp();
    dispatchClick(find(app.fieldsNode, 'toggle-mode', (item) => item.dataset.field === 'title'));
    assertFocused(app, find(app.fieldsNode, 'toggle-mode', (item) => item.dataset.field === 'title'), 'mode toggle');

    app = makeApp();
    dispatchClick(find(app.fieldsNode, 'toggle-photo', (item) => item.dataset.photoUrl === 'photo-a'));
    assertFocused(app, find(app.fieldsNode, 'toggle-photo', (item) => item.dataset.photoUrl === 'photo-a'), 'photo selection toggle');

    app = makeApp();
    dispatchClick(find(app.fieldsNode, 'remove-characteristic', (item) => item.dataset.charIndex === '0'));
    assertFocused(app, app.fieldsNode.querySelectorAll('[data-char-part="name"]')[0], 'characteristic removal');

    app = makeApp();
    dispatchClick(find(app.listNode, 'choose-product', (item) => item.dataset.productId === '22'));
    assertFocused(app, find(app.listNode, 'choose-product', (item) => item.dataset.productId === '22'), 'product chooser');

    app = makeApp();
    dispatchInput(app.fieldsNode.querySelector('[data-field-input="title"]'), 'Changed title');
    dispatchClick(find(app.fieldsNode, 'reset-product'));
    assertFocused(app, find(app.fieldsNode, 'toggle-mode'), 'reset action');

    app = makeApp();
    dispatchClick(find(app.fieldsNode, 'refresh-product'));
    await drain();
    const refreshed = find(app.fieldsNode, 'refresh-product');
    assert.ok(!refreshed.disabled, 'refresh control remains enabled after its busy render');
    assertFocused(app, refreshed, 'successful refresh');

    app = makeApp();
    const externalControl = app.document.mount('outside-editor-control', app.document.body, 'button');
    dispatchClick(find(app.fieldsNode, 'refresh-product'));
    externalControl.focus();
    await drain();
    assert.equal(app.document.activeElement === externalControl, true, 'refresh completion must not steal focus from outside the editor');

    app = makeApp({ getFailure: true });
    dispatchClick(find(app.fieldsNode, 'refresh-product'));
    await drain();
    assert.ok(!app.root.inert, 'refresh error should be exposed only after busy state ends');
    assert.ok(!app.errorNode.hidden, 'refresh error should remain visible');
    assertFocused(app, app.errorNode, 'refresh error');
}

async function run() {
    await photoBoundaryFocus();
    await previewReturnDoesNotReuseToken();
    await localReplacementFocusInventory();
    process.stdout.write(JSON.stringify({
        status: 'passed',
        checks: [
            'common_photo_boundary_focus_first',
            'common_photo_boundary_focus_last',
            'common_preview_cancel_focus_return',
            'successful_apply_restores_focus',
            'saved_but_readback_failed_restores_focus_and_keeps_status',
            'apply_completion_does_not_steal_external_focus',
            'mode_photo_characteristic_product_reset_focus_restored',
            'refresh_success_and_error_focus_restored_after_busy',
            'refresh_completion_does_not_steal_external_focus',
        ],
        source: path.relative(process.cwd(), productionPath),
    }) + '\n');
}

run().catch((error) => {
    process.stderr.write((error && error.stack) || String(error));
    process.stderr.write('\n');
    process.exitCode = 1;
});
