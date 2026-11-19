"""Deterministic focus-navigation checks for the production Ozon editor method."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SETUP = r'''
const assert = require('node:assert/strict');
global.window = global;
const elements = new Map();
const attributeFields = [], groups = [], groupButtons = [], mediaItems = [];
const focused = [], scrolled = [];
function element(id, dataset = {}, options = {}) {
    const details = options.details || null;
    const control = options.control || null;
    const item = {
        id, dataset, disabled: !!options.disabled, fieldsetDisabled: !!options.fieldsetDisabled, open: false,
        focus() { focused.push(id); },
        scrollIntoView() { scrolled.push(id); },
        matches(selector) { return selector === ':disabled' && (this.disabled || this.fieldsetDisabled || !!options.inheritedDisabled); },
        querySelector(selector) {
            if (selector === 'summary') return options.summary || null;
            if (selector.includes(':not(:disabled)')) return control && !control.matches(':disabled') ? control : null;
            return null;
        },
        closest(selector) { return selector === 'details' ? details : null; },
    };
    if (id) elements.set(id, item);
    return item;
}
global.document = {
    getElementById(id) { return elements.get(id) || null; },
    querySelectorAll(selector) {
        if (selector === '.ode-attribute') return attributeFields;
        if (selector === '.ode-complex-group') return groups;
        if (selector === '[data-complex-type-id]') return groupButtons;
        if (selector === '.ode-media-item') return mediaItems;
        return [];
    },
};
require('./static/ozon-draft-editor.js');
const options = ozonDraftEditor.createOptions({enabled:true, publicationEnabled:true, urls:{}});
const page = options.data();
for (const [key, value] of Object.entries(options.methods)) page[key] = value.bind(page);
const ticks = [];
page.$nextTick = callback => ticks.push(callback);
function navigate(issue) {
    focused.length = 0; scrolled.length = 0;
    page.goToIssue(issue);
    assert.equal(ticks.length, 1, 'focus waits for Vue render');
    ticks.shift()();
    return focused[0] || null;
}
'''


def run_node(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for Vue behavior checks")
    result = subprocess.run(
        [node, "-e", SETUP + source], cwd=ROOT, capture_output=True,
        text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_complex_issue_targets_the_exact_repeated_group_without_editing_form():
    run_node(r'''
const summary = element('complex-summary');
const details = element('ode-complex-groups', {}, {summary});
const control0 = element('complex-control-0');
const control1 = element('complex-control-1');
const field0 = element('complex-field-0', {attribute:'8111', complexGroupIndex:'0'}, {details, control:control0});
const field1 = element('complex-field-1', {attribute:'8111', complexGroupIndex:'1'}, {details, control:control1});
attributeFields.push(field0, field1);
groups.push(element('complex-group-0', {complexGroupIndex:'0'}), element('complex-group-1', {complexGroupIndex:'1'}));
page.data = {definitions:[{id:'8111', complex_id:'57'}]};
page.form = {attributes:[], complex_attributes:[
    {attributes:[{attribute_id:'8111', complex_id:'57', values:[]}]},
    {attributes:[{attribute_id:'8111', complex_id:'57', values:[]}]},
]};
const before = JSON.stringify(page.form);
assert.equal(navigate({field:'complex_attributes[1].attributes[0].values[0]', code:'dictionary_value_id_missing'}), 'complex-control-1');
assert.equal(details.open, true, 'collapsed complex section opens for navigation');
assert.equal(JSON.stringify(page.form), before, 'navigation leaves draft values untouched');
assert.equal(page.section, 'attributes');
''')


def test_missing_complex_group_focuses_matching_add_control_and_keeps_locked_state():
    run_node(r'''
const summary = element('complex-summary');
const details = element('ode-complex-groups', {}, {summary});
const add58 = element('add-group-58', {complexTypeId:'58'});
const add57 = element('add-group-57', {complexTypeId:'57'});
groupButtons.push(add58, add57);
page.data = {definitions:[{id:'8111', complex_id:'57'}]};
page.form = {attributes:[], complex_attributes:[]};
const before = JSON.stringify(page.form);
assert.equal(navigate({field:'attributes.8111', code:'required_attribute_missing'}), 'add-group-57');
assert.equal(details.open, true);
assert.equal(JSON.stringify(page.form), before, 'focus does not create the missing group');

add57.fieldsetDisabled = true;
assert.equal(navigate({field:'attributes.8111', code:'required_attribute_missing'}), 'complex-summary');
assert.equal(add57.matches(':disabled'), true, 'locked control remains disabled by its fieldset');
assert.equal(JSON.stringify(page.form), before);
''')


def test_simple_4191_media_barcode_and_commercial_paths_focus_real_controls():
    run_node(r'''
const description = element('ode-description');
const contentHeading = element('ode-content-heading');
const vat = element('ode-vat');
const deliveryHeading = element('ode-delivery-heading');
const photoUrl = element('ode-photo-url');
const colorSummary = element('color-summary');
const colorDetails = element('color-details', {}, {summary:colorSummary});
const colorUrl = element('ode-color-url', {}, {details:colorDetails});
const barcode0 = element('ode-barcode-0');
const barcode1 = element('ode-barcode-1');
const addBarcode = element('ode-add-barcode');
const media0 = element('photo-primary', {primaryImage:'true', imageIndex:'-1'});
const media1 = element('photo-second', {primaryImage:undefined, imageIndex:'1'});
mediaItems.push(media0, media1);
page.data = {definitions:[{id:'8123', complex_id:'0'}]};
page.form = {attributes:[], complex_attributes:[], media:{images:['first','second'], primary_image:'cover'}, barcodes:['a','b']};
attributeFields.push(element('simple-attribute-8123', {attribute:'8123'}));

assert.equal(navigate({field:'attributes.8123', code:'required_attribute_missing'}), 'simple-attribute-8123');
assert.equal(page.attrQuery, '8123', 'simple definition is made visible by exact search');
assert.equal(navigate({field:'attributes.4191', code:'description_attribute_conflict'}), 'ode-description');
assert.equal(page.section, 'content', 'description attribute 4191 retains its special mapping');
assert.equal(navigate({field:'media', code:'images_required'}), 'ode-photo-url');
assert.equal(navigate({field:'media.images[1]', code:'image_duplicate'}), 'photo-second');
assert.equal(navigate({field:'media.color_image', code:'image_url_invalid'}), 'ode-color-url');
assert.equal(colorDetails.open, true);
assert.equal(navigate({field:'barcodes[1]', code:'barcode_duplicate'}), 'ode-barcode-1');
assert.equal(navigate({field:'barcodes', code:'barcodes_limit'}), 'ode-barcode-0');
page.form.barcodes = [];
elements.delete('ode-barcode-0');
assert.equal(navigate({field:'barcodes[0]', code:'barcode_invalid'}), 'ode-add-barcode');
assert.equal(navigate({field:'commercial.vat', code:'vat_required'}), 'ode-vat');
vat.fieldsetDisabled = true;
assert.equal(navigate({field:'commercial.vat', code:'vat_required'}), 'ode-delivery-heading');
description.fieldsetDisabled = true;
assert.equal(navigate({field:'attributes.4191', code:'description_attribute_conflict'}), 'ode-content-heading');
colorUrl.disabled = true;
assert.equal(navigate({field:'media.color_image', code:'image_url_invalid'}), 'color-summary');

page.form.media.images = ['https://bad.invalid/photo'];
mediaItems.length = 0;
assert.equal(navigate({field:'media.images[0]', code:'image_url_not_public'}), 'ode-photo-url', 'unrendered unsafe media falls back to the visible add-photo control');
page.form.media.images = ['duplicate', 'duplicate'];
mediaItems.push(element('photo-duplicate-first', {imageIndex:'0'}));
assert.equal(navigate({field:'media.images[1]', code:'image_duplicate'}), 'photo-duplicate-first', 'a deduplicated repeated URL maps to its visible card');
assert.equal(navigate({field:'unknown.provider_path', code:'unknown_issue'}), 'ode-content-heading', 'unknown issues retain a useful section fallback');
''')


def test_template_exposes_stable_targets_without_changing_issue_actions():
    template = (ROOT / "templates/marketplace_draft_detail.html").read_text()
    assert 'id="ode-complex-groups"' in template
    assert ':data-complex-type-id="type.id"' in template
    assert ':data-complex-group-index="groupIndex"' in template
    assert 'data-image-index="form.media.images.indexOf(photo)"' in template
    assert ':id="\'ode-barcode-\' + index"' in template
    assert '@click="form.barcodes.push(\'\')"' in template
