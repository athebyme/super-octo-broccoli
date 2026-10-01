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

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.capture = tag == "script" and values.get("id") == "common-content-bootstrap"

    def handle_endtag(self, tag):
        if tag == "script" and self.capture:
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def _wire_page_template(app):
    app.add_url_rule("/my-products", endpoint="seller_my_products", view_func=lambda: "catalog")
    app.jinja_loader = ChoiceLoader([
        DictLoader({"base.html": "<!doctype html><title>{% block title %}{% endblock %}</title>{% block content %}{% endblock %}"}),
        FileSystemLoader(str(ROOT / "templates")),
    ])
    app.jinja_env.cache.clear()


def test_page_renders_empty_and_selected_states_with_safe_bootstrap(api):
    app, client, (seller_id, user_id, own_id, _) = api
    _wire_page_template(app)
    user_patch, login_patch = _auth(seller_id, user_id)
    with user_patch, login_patch:
        empty = client.get("/my-products/common-content")
        with app.app_context():
            product = db.session.get(ImportedProduct, own_id)
            product.title = "</script><script>window.injected=true</script>"
            db.session.commit()
        selected = client.get(f"/my-products/common-content?product_id={own_id}")

    assert empty.status_code == 200
    assert "Сначала выберите товары" in empty.get_data(as_text=True)
    assert selected.status_code == 200
    html = selected.get_data(as_text=True)
    assert "Сохранится только общий товар в Seller Hub" in html
    assert "Черновики и опубликованные карточки на площадках останутся без изменений" in html
    assert "common-product-content.css" in html
    assert "common-product-content.js" in html
    assert "<pre" not in html
    assert "</script><script>window.injected" not in html

    parser = BootstrapParser()
    parser.feed(html)
    bootstrap = json.loads("".join(parser.parts))
    assert bootstrap["selectedProductIds"] == [own_id]
    assert len(bootstrap["products"]) == 1
    assert bootstrap["products"][0]["product_id"] == own_id


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
