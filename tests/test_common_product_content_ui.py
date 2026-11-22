"""Server-render and selection-boundary checks for the common-content UI."""

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

from models import ImportedProduct, db
from tests.test_common_product_content_routes import _auth, api


ROOT = Path(__file__).resolve().parents[1]


class BootstrapParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.parts = []
        self.csrf_meta_token = None
        self.workspace_links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "csrf-token":
            self.csrf_meta_token = values.get("content")
        if "data-workspace-return-link" in values:
            self.workspace_links.append(values.get("href"))
        self.capture = tag == "script" and values.get("id") == "common-content-bootstrap"

    def handle_endtag(self, tag):
        if tag == "script" and self.capture:
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def _wire_page_template(app):
    app.add_url_rule("/my-products", endpoint="seller_my_products", view_func=lambda: "catalog")
    app.add_url_rule("/my-products/beta", endpoint="seller_my_products_beta", view_func=lambda: "beta")
    # Load the real editor page template; keep the inherited base contract
    # that calls Flask-WTF's csrf_token() callable in the document metadata.
    app.jinja_loader = ChoiceLoader([
        DictLoader({"base.html": """<!doctype html><html><head>
            <meta name="csrf-token" content="{{ csrf_token() }}">
            <title>{% block title %}{% endblock %}</title>
            </head><body>{% block content %}{% endblock %}</body></html>"""}),
        FileSystemLoader(str(ROOT / "templates")),
    ])
    app.jinja_env.cache.clear()


def test_page_renders_empty_and_selected_states_with_safe_bootstrap(api):
    app, client, (seller_id, user_id, own_id, _) = api
    _wire_page_template(app)
    app.config["WTF_CSRF_ENABLED"] = True
    user_patch, login_patch = _auth(seller_id, user_id)
    with user_patch, login_patch:
        empty = client.get("/my-products/common-content")
        missing_csrf = client.post(
            "/api/my-products/common-content/preview",
            json={"items": []},
        )
        with app.app_context():
            product = db.session.get(ImportedProduct, own_id)
            product.title = "</script><script>window.injected=true</script>"
            db.session.commit()
        selected = client.get(f"/my-products/common-content?product_id={own_id}")

    assert empty.status_code == 200
    assert missing_csrf.status_code == 400
    assert "The CSRF token is missing." in missing_csrf.get_data(as_text=True)
    assert "Сначала выберите товары" in empty.get_data(as_text=True)
    assert selected.status_code == 200
    html = selected.get_data(as_text=True)
    assert "Данные товара" in html
    assert "Сохраняются только данные товара в Seller Hub" in html
    assert "Черновики и опубликованные карточки WB и Ozon не меняются" in html
    assert "общему товару" not in html
    assert "common-product-content.css" in html
    assert "grid-template-columns: minmax(6rem, .55fr) repeat(3, minmax(0, 1fr))" in html
    assert "#common-content-editor .cpc-preview-confirm input:focus-visible" in html
    assert "@media (max-width: 760px)" in html
    assert "common-product-content.js" in html
    assert "<pre" not in html
    assert "</script><script>window.injected" not in html

    empty_parser = BootstrapParser()
    empty_parser.feed(empty.get_data(as_text=True))
    empty_bootstrap = json.loads("".join(empty_parser.parts))
    assert empty_parser.csrf_meta_token
    assert empty_bootstrap["csrfToken"] == empty_parser.csrf_meta_token
    assert empty_bootstrap["workspaceUrl"] == "/my-products/beta"
    assert empty_bootstrap["workspaceOwner"] == f"{user_id}:{seller_id}"
    assert empty_parser.workspace_links == ["/my-products", "/my-products", "/my-products"]

    parser = BootstrapParser()
    parser.feed(html)
    bootstrap = json.loads("".join(parser.parts))
    assert parser.csrf_meta_token
    assert bootstrap["csrfToken"] == parser.csrf_meta_token
    assert bootstrap["workspaceUrl"] == "/my-products/beta"
    assert bootstrap["workspaceOwner"] == f"{user_id}:{seller_id}"
    assert bootstrap["selectedProductIds"] == [own_id]
    assert len(bootstrap["products"]) == 1
    assert bootstrap["products"][0]["product_id"] == own_id
    assert parser.workspace_links == ["/my-products", "/my-products"]


def test_selection_url_helper_enforces_repeated_product_ids_and_fifty_item_cap():
    node = shutil.which("node")
    assert node, "Node is required to verify the shared list-link helper"
    script = r"""
const assert = require('node:assert/strict');
const { selectionUrl, serializeFieldChange, readablePhotoOrder, photoPreviewFallbackLabel } = require('./static/common-product-content.js');
const url = selectionUrl('/my-products/common-content', [14, 7, 14]);
const parsed = new URL(url, 'http://seller.test');
assert.equal(parsed.pathname, '/my-products/common-content');
assert.deepEqual(parsed.searchParams.getAll('product_id'), ['14', '7']);
const fifty = selectionUrl('/my-products/common-content', Array.from({length: 50}, (_, i) => i + 1));
assert.equal(new URL(fifty, 'http://seller.test').searchParams.getAll('product_id').length, 50);
assert.throws(() => selectionUrl('/my-products/common-content', []), RangeError);
assert.throws(() => selectionUrl('/my-products/common-content', Array.from({length: 51}, (_, i) => i + 1)), RangeError);
assert.throws(() => selectionUrl('//outside.test/path', [1]), TypeError);
assert.throws(() => selectionUrl('/my-products/common-content', [true]), TypeError);
assert.deepEqual(serializeFieldChange('override', '', false, 'inherited text'), {mode: 'override', value: ''});
assert.deepEqual(serializeFieldChange('override', '', true, 'existing value'), {mode: 'override', value: ''});
assert.equal(serializeFieldChange('inherit', '', true, 'existing value').mode, 'inherit');
assert.equal(serializeFieldChange('override', '', true, ''), null);
const pool = [{url: 'photo-a'}, {url: 'photo-b'}];
assert.equal(readablePhotoOrder(['photo-a', 'photo-b'], pool), 'Фото 1 → Фото 2');
assert.equal(readablePhotoOrder(['photo-b', 'photo-a'], pool), 'Фото 2 → Фото 1');
assert.notEqual(
    readablePhotoOrder(['photo-a', 'photo-b'], pool),
    readablePhotoOrder(['photo-b', 'photo-a'], pool),
    'same photo count in a changed order must produce a visible diff',
);
assert.equal(photoPreviewFallbackLabel(true, false), 'Предпросмотр сохранённого фото недоступен');
assert.equal(photoPreviewFallbackLabel(true, true), 'Предпросмотр сохранённого фото недоступен');
assert.equal(photoPreviewFallbackLabel(false, false), 'Нет предпросмотра');
assert.equal(photoPreviewFallbackLabel(false, true), 'Предпросмотр недоступен');
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_workspace_return_capsule_is_single_slot_bounded_and_identity_bound():
    node = shutil.which("node")
    assert node, "Node is required to verify the workspace handoff helper"
    script = r"""
const assert = require('node:assert/strict');
const {
    storeWorkspaceReturn, workspaceReturnFromHash, consumeWorkspaceReturn,
    priceWorkspaceReturnHref, bindPriceWorkspaceResult, priceResultWorkspaceReturnHref,
    WORKSPACE_RETURN_SESSION_KEY,
} = require('./static/common-product-content.js');

function storage() {
    const values = new Map();
    return {
        values,
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, String(value)); },
        removeItem(key) { values.delete(key); },
    };
}
let randomByte = 0;
const crypto = { getRandomValues(bytes) {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = (randomByte + i) & 255;
    randomByte += 16;
    return bytes;
} };
const now = 1800000000000;
const owner = '41:9';
const payload = {
    owner,
    editorIds: [14, 7],
    selectionIds: [7, 14, 22],
    scope: { tab: 'all', search: 'склад', prefix: 'SKU', sort: 'title' },
    batch: 'signed-import-batch-token',
};
const saved = storage();
const options = { storage: saved, crypto, now };
const nonce = storeWorkspaceReturn(payload, options);
assert.match(nonce, /^[a-f0-9]{32}$/);
assert.equal(saved.values.size, 1, 'the capsule uses one fixed storage slot');
assert.ok(saved.getItem(WORKSPACE_RETURN_SESSION_KEY));
const capsule = JSON.parse(saved.getItem(WORKSPACE_RETURN_SESSION_KEY));
assert.equal(capsule.expiresAt, now + 60 * 60 * 1000);
assert.deepEqual(capsule.editorIds, [14, 7]);
assert.deepEqual(capsule.selectionIds, [7, 14, 22]);
assert.equal(capsule.batch, payload.batch);
const matched = workspaceReturnFromHash('#workspace-return=' + nonce, {
    ...options, owner, editorIds: [14, 7],
});
assert.deepEqual(matched.editorIds, [14, 7]);
assert.deepEqual(matched.selectionIds, [7, 14, 22]);
assert.deepEqual(matched.scope, payload.scope);
assert.equal(matched.batch, payload.batch);
assert.equal(workspaceReturnFromHash('#workspace-return=' + nonce, {
    ...options, owner, editorIds: [7, 14],
}), null, 'editor ID order must match the server bootstrap exactly');
assert.equal(saved.getItem(WORKSPACE_RETURN_SESSION_KEY), null, 'a mismatched editor handoff is discarded');
const malformedStorage = storage();
malformedStorage.setItem(WORKSPACE_RETURN_SESSION_KEY, JSON.stringify({ ...capsule, batch: 'unsafe/+' }));
assert.equal(workspaceReturnFromHash('#workspace-return=' + nonce, {
    ...options, storage: malformedStorage, owner, editorIds: [14, 7],
}), null, 'the same capsule validator rejects invalid stored shapes');

const ownerBound = storage();
const ownerNonce = storeWorkspaceReturn(payload, { ...options, storage: ownerBound });
assert.equal(workspaceReturnFromHash('#workspace-return=' + ownerNonce, {
    ...options, storage: ownerBound, owner: '41:10', editorIds: [14, 7],
}), null, 'a different user or seller cannot use the capsule');
assert.equal(ownerBound.getItem(WORKSPACE_RETURN_SESSION_KEY), null, 'a mismatched identity is discarded');

const betaRead = storage();
const betaNonce = storeWorkspaceReturn(payload, { ...options, storage: betaRead });
const betaEnvelope = workspaceReturnFromHash('#workspace-return=' + betaNonce, {
    storage: betaRead, now, owner,
});
assert.equal(betaEnvelope.nonce, betaNonce, 'the Beta landing can validate owner and nonce before consuming');
assert.equal(consumeWorkspaceReturn(betaNonce, { storage: betaRead }), true);
assert.equal(betaRead.getItem(WORKSPACE_RETURN_SESSION_KEY), null, 'the consumer removes the one-time capsule');
assert.equal(consumeWorkspaceReturn(betaNonce, { storage: betaRead }), false);

const expiring = storage();
const expiringNonce = storeWorkspaceReturn(payload, { ...options, storage: expiring });
assert.equal(workspaceReturnFromHash('#workspace-return=' + expiringNonce, {
    storage: expiring, now: now + 60 * 60 * 1000, owner, editorIds: [14, 7],
}), null, 'the capsule expires after one hour');
assert.equal(expiring.getItem(WORKSPACE_RETURN_SESSION_KEY), null);

const singleSlot = storage();
const firstNonce = storeWorkspaceReturn(payload, { ...options, storage: singleSlot });
const secondNonce = storeWorkspaceReturn(payload, { ...options, storage: singleSlot });
assert.notEqual(firstNonce, secondNonce);
assert.equal(singleSlot.values.size, 1);
assert.equal(workspaceReturnFromHash('#workspace-return=' + firstNonce, {
    ...options, storage: singleSlot, owner, editorIds: [14, 7],
}), null, 'a newer handoff replaces the only stored capsule');
assert.equal(consumeWorkspaceReturn(firstNonce, { storage: singleSlot }), false);
assert.equal(consumeWorkspaceReturn(secondNonce, { storage: singleSlot }), true);

assert.equal(storeWorkspaceReturn({ ...payload, editorIds: Array.from({length: 51}, (_, i) => i + 1) }, options), null);
assert.equal(storeWorkspaceReturn({ ...payload, selectionIds: Array.from({length: 201}, (_, i) => i + 1) }, options), null);
const emptySelection = storage();
const emptySelectionNonce = storeWorkspaceReturn({ ...payload, selectionIds: [] }, { ...options, storage: emptySelection });
assert.deepEqual(workspaceReturnFromHash('#workspace-return=' + emptySelectionNonce, {
    ...options, storage: emptySelection, owner, editorIds: [14, 7],
}).selectionIds, [], 'an empty workspace selection is a valid state');
assert.equal(storeWorkspaceReturn({ ...payload, batch: 'x'.repeat(8193) }, options), null);
assert.equal(storeWorkspaceReturn({ ...payload, batch: '' }, options), null);
assert.equal(storeWorkspaceReturn({ ...payload, batch: 'unsafe/+' }, options), null);
assert.equal(storeWorkspaceReturn({ ...payload, owner: 'user:9' }, options), null);
assert.equal(storeWorkspaceReturn({ ...payload, scope: { data: 'x'.repeat(16 * 1024) } }, options), null);
assert.equal(workspaceReturnFromHash('#workspace-return=not-a-nonce', {
    ...options, owner, editorIds: [14, 7],
}), null);
assert.equal(storeWorkspaceReturn(payload, { storage: storage(), crypto: {}, now }), null,
    'nonce generation must use cryptographic randomness');

const wbTarget = {channel: 'wb', ids: [4401, 4402], accountId: null};
const pricePayload = {
    owner,
    priceTarget: wbTarget,
    selectionIds: [7, 14],
    scope: {
        activeTab: '', search: 'coat', supplier: 3,
        has_photos: 'yes', stock: '', sort: 'title', visibleCount: 50,
    },
    batch: 'signed-import-batch-token',
};
const priceStorage = storage();
const priceOptions = {...options, storage: priceStorage};
const priceNonce = storeWorkspaceReturn(pricePayload, priceOptions);
assert.match(priceNonce, /^[a-f0-9]{32}$/);
assert.equal(priceStorage.values.size, 1, 'price and editor capsules share the existing single slot');
const priceCapsule = JSON.parse(priceStorage.getItem(WORKSPACE_RETURN_SESSION_KEY));
assert.equal(priceCapsule.v, 2);
assert.equal(Object.hasOwn(priceCapsule, 'editorIds'), false);
assert.deepEqual(priceCapsule.priceTarget, wbTarget);
assert.deepEqual(priceCapsule.selectionIds, [7, 14]);
assert.deepEqual(priceCapsule.scope, pricePayload.scope);
assert.equal(priceCapsule.batch, pricePayload.batch);
assert.deepEqual(workspaceReturnFromHash('#workspace-return=' + priceNonce, {
    ...priceOptions, owner,
}), priceCapsule, 'the Beta landing accepts both capsule variants without a target argument');
assert.deepEqual(workspaceReturnFromHash('#workspace-return=' + priceNonce, {
    ...priceOptions, owner, priceTarget: wbTarget,
}), priceCapsule, 'the exact channel and ordered target IDs validate');

const priceHref = priceWorkspaceReturnHref(
    '#workspace-return=' + priceNonce,
    {owner, priceTarget: wbTarget, workspaceUrl: '/my-products/beta'},
    priceOptions,
);
const parsedPriceHref = new URL(priceHref, 'https://seller.test');
assert.equal(parsedPriceHref.origin, 'https://seller.test');
assert.equal(parsedPriceHref.pathname, '/my-products/beta');
assert.equal(parsedPriceHref.searchParams.getAll('import_batch').length, 1);
assert.equal(parsedPriceHref.searchParams.get('import_batch'), pricePayload.batch);
assert.equal(parsedPriceHref.hash, '#workspace-return=' + priceNonce);
assert.ok(priceStorage.getItem(WORKSPACE_RETURN_SESSION_KEY),
    'building a return link does not consume the one-time capsule before Beta re-reads it');
assert.equal(priceWorkspaceReturnHref(
    '#workspace-return=' + priceNonce,
    {owner, priceTarget: wbTarget, workspaceUrl: 'https://outside.test/my-products/beta'},
    priceOptions,
), '', 'return path is fixed to the exact same-site Beta route');

const wbHash = '#workspace-return=' + priceNonce;
const wbResult = {channel: 'wb', batchId: 8324};
const wbResultPayload = {owner, priceResult: wbResult, workspaceUrl: '/my-products/beta'};
assert.equal(priceResultWorkspaceReturnHref(wbHash, wbResultPayload, priceOptions), '',
    'an original price selection alone does not authorize a return link on any batch');
assert.equal(bindPriceWorkspaceResult(wbHash, {
    owner, priceTarget: wbTarget, priceResult: {...wbResult, batchId: '8324'},
}, priceOptions), '');
assert.equal(bindPriceWorkspaceResult(wbHash, {
    owner, priceTarget: wbTarget, priceResult: wbResult,
}, priceOptions), wbHash);
const boundWbCapsule = workspaceReturnFromHash(wbHash, {...priceOptions, owner});
assert.deepEqual(boundWbCapsule, {...priceCapsule, v: 3, priceResult: wbResult},
    'binding a result preserves the original selection, filters, nonce, and expiry');
assert.equal(priceResultWorkspaceReturnHref(wbHash, wbResultPayload, priceOptions), priceHref);
assert.equal(priceWorkspaceReturnHref(wbHash, {owner, priceTarget: wbTarget, workspaceUrl: '/my-products/beta'},
    priceOptions), priceHref, 'history can return to the exact original price form');
assert.equal(priceResultWorkspaceReturnHref(wbHash, {
    ...wbResultPayload, priceResult: {...wbResult, batchId: 8325},
}, priceOptions), '', 'another owned WB batch cannot reuse the result link');
assert.equal(priceResultWorkspaceReturnHref(wbHash, wbResultPayload, priceOptions), priceHref,
    'visiting another owned batch does not discard the current return');
assert.equal(priceResultWorkspaceReturnHref(wbHash, wbResultPayload, {
    ...priceOptions, now: boundWbCapsule.expiresAt,
}), '', 'result binding does not extend expiry');
assert.equal(priceStorage.getItem(WORKSPACE_RETURN_SESSION_KEY), null);

const targetMismatch = storage();
const mismatchNonce = storeWorkspaceReturn(pricePayload, {...options, storage: targetMismatch});
assert.equal(workspaceReturnFromHash('#workspace-return=' + mismatchNonce, {
    ...options, storage: targetMismatch, owner,
    priceTarget: {channel: 'wb', ids: [4402, 4401], accountId: null},
}), null, 'price target order is part of the bound handoff');
assert.equal(targetMismatch.getItem(WORKSPACE_RETURN_SESSION_KEY), null,
    'a same-nonce target mismatch discards the capsule');

const wrongChannel = storage();
const wrongChannelNonce = storeWorkspaceReturn(pricePayload, {...options, storage: wrongChannel});
assert.equal(priceWorkspaceReturnHref(
    '#workspace-return=' + wrongChannelNonce,
    {owner, priceTarget: {channel: 'ozon', ids: [4401, 4402], accountId: 9}, workspaceUrl: '/my-products/beta'},
    {...options, storage: wrongChannel},
), '', 'the return helper rejects a different channel');
assert.equal(wrongChannel.getItem(WORKSPACE_RETURN_SESSION_KEY), null,
    'the mismatched price return consumes the matching nonce');

const ozonTarget = {channel: 'ozon', ids: [9101, 9102], accountId: 32};
const ozonStorage = storage();
const ozonNonce = storeWorkspaceReturn({...pricePayload, priceTarget: ozonTarget}, {
    ...options, storage: ozonStorage,
});
const ozonHref = priceWorkspaceReturnHref(
    '#workspace-return=' + ozonNonce,
    {owner, priceTarget: ozonTarget, workspaceUrl: '/my-products/beta'},
    {...options, storage: ozonStorage},
);
assert.equal(new URL(ozonHref, 'https://seller.test').pathname, '/my-products/beta');
assert.equal(workspaceReturnFromHash('#workspace-return=' + ozonNonce, {
    ...options, storage: ozonStorage, owner,
    priceTarget: {channel: 'ozon', ids: [9101, 9102], accountId: 33},
}), null, 'the selected Ozon account is part of the exact return target');
assert.equal(ozonStorage.getItem(WORKSPACE_RETURN_SESSION_KEY), null);

const ozonResultStorage = storage();
const ozonResultOptions = {...options, storage: ozonResultStorage};
const ozonResultHash = '#workspace-return=' + storeWorkspaceReturn({
    ...pricePayload, priceTarget: ozonTarget,
}, ozonResultOptions);
const ozonResult = {channel: 'ozon', accountId: 32, proposalIds: [4305, 4302]};
const bindOzonPayload = {owner, priceTarget: ozonTarget, priceResult: ozonResult};
const ozonResultPayload = {owner, priceResult: ozonResult, workspaceUrl: '/my-products/beta'};
assert.equal(bindPriceWorkspaceResult(ozonResultHash, {
    ...bindOzonPayload, priceResult: {...ozonResult, accountId: 33},
}, ozonResultOptions), '', 'a receipt for a different account cannot bind a result');
assert.equal(bindPriceWorkspaceResult(ozonResultHash, {
    ...bindOzonPayload, priceResult: {...ozonResult, proposalIds: [4302, 4305, 4306]},
}, ozonResultOptions), '', 'the number of saved proposals cannot exceed the source selection');
assert.equal(bindPriceWorkspaceResult(ozonResultHash, bindOzonPayload, ozonResultOptions), ozonResultHash);
const boundOzonCapsule = workspaceReturnFromHash(ozonResultHash, {...ozonResultOptions, owner});
assert.deepEqual(boundOzonCapsule.priceResult, {...ozonResult, proposalIds: [4302, 4305]},
    'result IDs represent an unordered exact set');
assert.equal(priceResultWorkspaceReturnHref(ozonResultHash, ozonResultPayload, ozonResultOptions),
    '/my-products/beta?import_batch=signed-import-batch-token' + ozonResultHash);
for (const wrongResult of [
    {...ozonResult, accountId: 33},
    {...ozonResult, proposalIds: [4302]},
    {...ozonResult, proposalIds: [4302, 4302]},
    {...ozonResult, proposalIds: [4302, 4306]},
    wbResult,
]) {
    assert.equal(priceResultWorkspaceReturnHref(ozonResultHash, {
        ...ozonResultPayload, priceResult: wrongResult,
    }, ozonResultOptions), '', 'restoring a matching price result requires the entire saved set and account');
}
assert.equal(priceResultWorkspaceReturnHref(ozonResultHash, {
    ...ozonResultPayload, owner: '44:9',
}, ozonResultOptions), '', 'another owner cannot restore the same exact Ozon result');
assert.equal(ozonResultStorage.getItem(WORKSPACE_RETURN_SESSION_KEY), null);

assert.equal(storeWorkspaceReturn({...pricePayload, priceTarget: {
    channel: 'wb', ids: [1], accountId: 9,
}}, options), null, 'WB target cannot carry an Ozon account');
assert.equal(storeWorkspaceReturn({...pricePayload, priceTarget: {
    channel: 'ozon', ids: [1], accountId: null,
}}, options), null, 'Ozon target must bind a positive account ID');
assert.equal(storeWorkspaceReturn({...pricePayload, priceTarget: {
    channel: 'ozon', ids: Array.from({length: 101}, (_, i) => i + 1), accountId: 9,
}}, options), null, 'Ozon price target is bounded to 100 listings');
assert.equal(storeWorkspaceReturn({...pricePayload, selectionIds: [7], priceTarget: wbTarget}, options), null,
    'one-to-one price returns reject a selection whose count differs from the target count');
assert.equal(storeWorkspaceReturn({...pricePayload, selectionIds: [], priceTarget: wbTarget}, options), null,
    'a price return cannot restore an empty original selection');
const malformedPriceStorage = storage();
malformedPriceStorage.setItem(WORKSPACE_RETURN_SESSION_KEY, JSON.stringify({
    ...priceCapsule,
    priceTarget: {...wbTarget, ignored: true},
}));
assert.equal(workspaceReturnFromHash('#workspace-return=' + priceNonce, {
    ...priceOptions, storage: malformedPriceStorage, owner,
}), null, 'malformed copied price-target keys fail stored-capsule validation');
assert.equal(storeWorkspaceReturn({...pricePayload, batch: 'x'.repeat(8193)}, options), null,
    'the signed batch capability keeps the existing 8192-byte bound');
assert.equal(storeWorkspaceReturn(pricePayload, {storage: storage(), crypto: {}, now}), null,
    'price returns also require a cryptographic nonce');
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_wb_edit_workspace_return_binds_nullable_account_and_ordered_ids():
    node = shutil.which("node")
    assert node, "Node is required to verify the WB workspace return capsule"
    script = r"""
const assert = require('node:assert/strict');
const {
  storeWorkspaceReturn, workspaceReturnFromHash, WORKSPACE_RETURN_SESSION_KEY,
} = require('./static/common-product-content.js');
function storage() {
  const values = new Map();
  return {
    values,
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
}
let randomByte = 0;
const crypto = {getRandomValues(bytes) {
  for (let i = 0; i < bytes.length; i += 1) bytes[i] = (randomByte + i) & 255;
  randomByte += 16;
  return bytes;
}};
const owner = '7:9';
const wbEditTarget = {accountId: null, productIds: [4402, 4401]};
const payload = {
  owner, wbEditTarget, selectionIds: [12, 11],
  scope: {
    activeTab: '', search: 'Pipedream', supplier: '', has_photos: '',
    stock: '', sort: 'title', visibleCount: 50,
  },
  batch: 'signed.import-batch_token',
};
const saved = storage();
const options = {storage: saved, crypto, now: 1800000000000};
const nonce = storeWorkspaceReturn(payload, options);
assert.match(nonce, /^[a-f0-9]{32}$/);
const capsule = JSON.parse(saved.getItem(WORKSPACE_RETURN_SESSION_KEY));
assert.equal(capsule.v, 5);
assert.equal(Object.hasOwn(capsule, 'editorIds'), false,
  'WB edit has its own typed purpose instead of using the 50-ID common-content purpose');
assert.deepEqual(capsule.wbEditTarget, wbEditTarget);
assert.deepEqual(capsule.selectionIds, payload.selectionIds);
assert.equal(capsule.batch, payload.batch);
assert.deepEqual(workspaceReturnFromHash('#workspace-return=' + nonce, {
  ...options, owner, wbEditTarget,
}), capsule, 'the return page checks account and exact ordered WB IDs');

for (const mismatch of [
  {accountId: 'new-wb-account', productIds: [4402, 4401]},
  {accountId: null, productIds: [4401, 4402]},
  {accountId: null, productIds: [4402]},
]) {
  const copy = storage();
  const copyNonce = storeWorkspaceReturn(payload, {...options, storage: copy});
  assert.equal(workspaceReturnFromHash('#workspace-return=' + copyNonce, {
    ...options, storage: copy, owner, wbEditTarget: mismatch,
  }), null, 'a changed WB account, order, or count clears the exact return capsule');
  assert.equal(copy.getItem(WORKSPACE_RETURN_SESSION_KEY), null);
}

for (const invalid of [
  {accountId: '', productIds: [1]},
  {accountId: ' account ', productIds: [1]},
  {accountId: 'x'.repeat(101), productIds: [1]},
  {accountId: null, productIds: [1, 1]},
  {accountId: null, productIds: Array.from({length: 201}, (_, i) => i + 1)},
  {accountId: null, productIds: [1], ignored: true},
]) {
  assert.equal(storeWorkspaceReturn({...payload, wbEditTarget: invalid}, options), null);
}
assert.equal(storeWorkspaceReturn({...payload, selectionIds: [12]}, options), null,
  'the capsule requires one linked WB product per source selection');
const namedAccountNonce = storeWorkspaceReturn({...payload, wbEditTarget: {
  accountId: 'synthetic-account', productIds: [4402, 4401],
}}, {...options, storage: storage()});
assert.match(namedAccountNonce, /^[a-f0-9]{32}$/,
  'a configured WB account ID is also retained exactly');
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_ozon_preparation_workspace_return_binds_exact_job_and_source_scope():
    node = shutil.which("node")
    assert node, "Node is required to verify the Ozon preparation return helper"
    script = r"""
const assert = require('node:assert/strict');
const {
    storeWorkspaceReturn, workspaceReturnFromHash,
    bindOzonPreparationWorkspaceResult, ozonPreparationWorkspaceReturnHref,
    WORKSPACE_RETURN_SESSION_KEY,
} = require('./static/common-product-content.js');

function storage() {
    const values = new Map();
    return {
        values,
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, String(value)); },
        removeItem(key) { values.delete(key); },
    };
}
let randomByte = 0;
const crypto = { getRandomValues(bytes) {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = (randomByte + i) & 255;
    randomByte += 16;
    return bytes;
} };
const now = 1800000000000;
const owner = '41:9';
const jobUid = 'ozon-upload-' + 'a'.repeat(32);
const otherJobUid = 'ozon-upload-' + 'b'.repeat(32);
const prepareTarget = {accountId: 32, productIds: [301, 7, 22]};
const payload = {
    owner,
    prepareTarget,
    selectionIds: [],
    scope: {tab: 'all', search: 'крем', sort: 'title', page: 4},
    batch: 'signed-import-batch-token',
};
const saved = storage();
const options = {storage: saved, crypto, now};
const nonce = storeWorkspaceReturn(payload, options);
assert.match(nonce, /^[a-f0-9]{32}$/);
const hash = '#workspace-return=' + nonce;
const pending = JSON.parse(saved.getItem(WORKSPACE_RETURN_SESSION_KEY));
assert.equal(pending.v, 4);
assert.equal(pending.prepareJobUid, null);
assert.deepEqual(pending.prepareTarget, prepareTarget);
assert.deepEqual(pending.selectionIds, payload.selectionIds,
    'empty original selection is preserved independently from the exact prep target');
assert.deepEqual(pending.scope, payload.scope);
assert.equal(pending.batch, payload.batch);
assert.equal(workspaceReturnFromHash(hash, {...options, owner}).nonce, nonce,
    'My Products can read v4 capsules with the regular owner and nonce check');
assert.equal(globalThis.SellerHubCommonContent.bindOzonPreparationWorkspaceResult,
    bindOzonPreparationWorkspaceResult);
assert.equal(globalThis.SellerHubCommonContent.ozonPreparationWorkspaceReturnHref,
    ozonPreparationWorkspaceReturnHref);

const beforeInvalidBinding = saved.getItem(WORKSPACE_RETURN_SESSION_KEY);
for (const invalid of [
    {owner, prepareTarget, jobUid: 'wrong-shape'},
    {owner: '55:4', prepareTarget, jobUid},
    {owner, prepareTarget: {accountId: 33, productIds: [301, 7, 22]}, jobUid},
    {owner, prepareTarget: {accountId: 32, productIds: [7, 301, 22]}, jobUid},
]) {
    assert.equal(bindOzonPreparationWorkspaceResult(hash, invalid, options), '');
    assert.equal(saved.getItem(WORKSPACE_RETURN_SESSION_KEY), beforeInvalidBinding,
        'a bad binding attempt must not consume or alter the valid pending capsule');
}
assert.equal(bindOzonPreparationWorkspaceResult(hash, {
    owner, prepareTarget, jobUid,
}, options), hash);
const bound = JSON.parse(saved.getItem(WORKSPACE_RETURN_SESSION_KEY));
assert.equal(bound.v, 4);
assert.equal(bound.prepareJobUid, jobUid);
assert.equal(bound.expiresAt, pending.expiresAt, 'binding never extends the existing TTL');
assert.deepEqual(bound.prepareTarget, pending.prepareTarget);
assert.deepEqual(bound.selectionIds, pending.selectionIds);
assert.deepEqual(bound.scope, pending.scope);
assert.equal(bound.batch, pending.batch);
assert.equal(bindOzonPreparationWorkspaceResult(hash, {
    owner, prepareTarget, jobUid,
}, options), hash, 'same exact result binding is idempotent');
const afterValidBinding = saved.getItem(WORKSPACE_RETURN_SESSION_KEY);
assert.equal(bindOzonPreparationWorkspaceResult(hash, {
    owner, prepareTarget, jobUid: otherJobUid,
}, options), '', 'a different result job cannot rebind the capsule');
assert.equal(saved.getItem(WORKSPACE_RETURN_SESSION_KEY), afterValidBinding);

const returnPayload = {
    workspaceUrl: '/my-products/beta', owner, accountId: 32,
    productIds: [301, 7, 22], jobUid,
};
const href = '/my-products/beta?import_batch=' + payload.batch + hash;
assert.equal(ozonPreparationWorkspaceReturnHref(
    returnPayload, options,
), href);

const boundCapsuleBytes = saved.getItem(WORKSPACE_RETURN_SESSION_KEY);
for (const invalid of [
    {...returnPayload, jobUid: otherJobUid},
    {...returnPayload, owner: '55:4'},
    {...returnPayload, accountId: 33},
    {...returnPayload, productIds: [7, 301, 22]},
    {...returnPayload, productIds: [301, 7]},
    {...returnPayload, workspaceUrl: 'https://outside.test/my-products/beta'},
]) {
    assert.equal(ozonPreparationWorkspaceReturnHref(invalid, options), '');
    assert.equal(saved.getItem(WORKSPACE_RETURN_SESSION_KEY), boundCapsuleBytes,
        'a different history job or foreign target must not consume the valid capsule');
}
assert.equal(ozonPreparationWorkspaceReturnHref(returnPayload, options), href,
    'a detail/history page without a URL hash can restore its exact bound capsule');
assert.equal(ozonPreparationWorkspaceReturnHref(returnPayload, {
    ...options, hash, now: bound.expiresAt,
}), '', 'expired capsules cannot produce a restoration link');
assert.equal(saved.getItem(WORKSPACE_RETURN_SESSION_KEY), boundCapsuleBytes,
    'expiry fallback is non-destructive to the stored slot');

const unboundStorage = storage();
const unboundNonce = storeWorkspaceReturn(payload, {...options, storage: unboundStorage});
const unboundHash = '#workspace-return=' + unboundNonce;
assert.equal(ozonPreparationWorkspaceReturnHref(returnPayload, {
    ...options, storage: unboundStorage, hash: unboundHash,
}), '', 'an unbound preparation has no result return link');
assert.ok(unboundStorage.getItem(WORKSPACE_RETURN_SESSION_KEY),
    'an unbound scope remains available for the real result binding');

const maxTarget = {
    accountId: 32,
    productIds: Array.from({length: 200}, (_, index) => 1000 + index),
};
const maxStorage = storage();
const maxOptions = {...options, storage: maxStorage};
const maxNonce = storeWorkspaceReturn({
    owner, prepareTarget: maxTarget, selectionIds: [], scope: {}, batch: null,
}, maxOptions);
assert.match(maxNonce, /^[a-f0-9]{32}$/);
const maxHash = '#workspace-return=' + maxNonce;
assert.equal(bindOzonPreparationWorkspaceResult(maxHash, {
    owner, prepareTarget: maxTarget, jobUid,
}, maxOptions), maxHash);
assert.equal(ozonPreparationWorkspaceReturnHref({
    workspaceUrl: '/my-products/beta', owner,
    accountId: 32, productIds: maxTarget.productIds, jobUid,
}, maxOptions), '/my-products/beta' + maxHash);
for (const invalidTarget of [
    {accountId: 32, productIds: []},
    {accountId: 32, productIds: [1, 1]},
    {accountId: 32, productIds: [true]},
    {accountId: 32, productIds: Array.from({length: 201}, (_, index) => index + 1)},
    {accountId: 0, productIds: [1]},
    {accountId: 32, productIds: [1], ignored: true},
]) {
    assert.equal(storeWorkspaceReturn({
        owner, prepareTarget: invalidTarget, selectionIds: [], scope: {}, batch: null,
    }, options), null);
}
assert.equal(storeWorkspaceReturn({
    ...payload, editorIds: [7],
}, options), null, 'preparation target is exclusive with editor targets');
assert.equal(storeWorkspaceReturn({
    ...payload, priceTarget: {channel: 'wb', accountId: null, ids: [7]},
}, options), null, 'preparation target is exclusive with price targets');
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_production_editor_handlers_restore_native_focus_after_dom_replacement():
    node = shutil.which("node")
    assert node, "Node is required to exercise the production editor event handlers"
    harness = ROOT / "tests/ux01/common_product_content_focus_dom.js"
    result = subprocess.run(
        [node, str(harness)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "passed"
    assert set(report["checks"]) >= {
        "common_photo_boundary_focus_first",
        "common_photo_boundary_focus_last",
        "common_preview_cancel_focus_return",
        "common_preview_inherited_saved_proposed_provenance",
        "successful_apply_restores_focus",
        "saved_but_readback_failed_restores_focus_and_keeps_status",
        "apply_completion_does_not_steal_external_focus",
        "mode_photo_characteristic_product_reset_focus_restored",
        "refresh_success_and_error_focus_restored_after_busy",
        "refresh_completion_does_not_steal_external_focus",
        "common_photo_pending_recovery_is_bounded_and_write_free",
        "common_photo_exhaustion_offers_keyboard_manual_retry_in_inherit_mode",
        "common_photo_ready_state_survives_preview_cancel_and_refresh",
        "common_photo_lazy_deadline_waits_for_intersection",
        "common_photo_visibility_rerender_product_switch_and_pagehide_cancel_stale_work",
    }
