"""Regression checks for provider moderation presentation and listing images."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "static/marketplace-detail-beta.js"
TEMPLATE = ROOT / "templates/marketplace_listing_beta_detail.html"


def _run_component(assertions):
    bootstrap = r"""
const assert = require('node:assert/strict');
let appOptions;
global.document = {
  getElementById(id) {
    return id === 'mdet-bootstrap' ? {textContent: '{"listingId":40018}'} : {};
  },
  querySelector() { return null; },
  hidden: false,
  addEventListener() {},
  removeEventListener() {}
};
global.window = {
  mcatShared: {imageDeadline: {}, ozonPrices: {}},
  addEventListener() {},
  removeEventListener() {}
};
global.Vue = {
  createApp(options) {
    appOptions = options;
    return {mount() { return {}; }};
  }
};
"""
    script = SCRIPT.read_text(encoding="utf-8")
    result = subprocess.run(
        ["node", "-e", bootstrap + script + "\n" + assertions],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_actual_nested_unicode_json_selects_actionable_description_and_keeps_raw_secondary():
    _run_component(r"""
const nested = JSON.stringify({
  code: 'DESCRIPTION_DECLINE',
  message: 'Request failed',
  error: JSON.stringify({
    description: 'Описание содержит слишком длинные слова. Разделите склеенные слова.'
  })
});
const escapedUnicode = nested.replace(/[\u0400-\u04FF]/g,
  character => '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'));
const wrapped = JSON.stringify(escapedUnicode);
const presented = appOptions.computed.moderationErrors.call({listing: {
  marketplace_code: 'ozon', moderation_errors: [wrapped]
}})[0];
assert.equal(presented.summary, 'Слишком длинные слова в описании.');
assert.match(presented.nextStep, /Разделите склеенные слова/);
assert.match(presented.nextStep, /черновики Ozon/);
assert.match(presented.nextStep, /отправка карточки остаётся отдельным действием/);
assert.match(presented.technical, /DESCRIPTION_DECLINE/);
assert.match(presented.technical, /\\u041e/);

const wb = appOptions.computed.moderationErrors.call({listing: {
  marketplace_code: 'wb', moderation_errors: [wrapped]
}})[0];
assert.match(wb.nextStep, /сохранённую историю/);
assert.doesNotMatch(wb.nextStep, /Ozon/);
""")


def test_unknown_malformed_overlong_deep_and_non_matching_declines_stay_diagnostic():
    _run_component(r"""
const present = entry => appOptions.computed.moderationErrors.call({listing: {
  marketplace_code: 'ozon', moderation_errors: [entry]
}})[0];

const nonMatching = present({code: 'DESCRIPTION_DECLINE',
  description: 'Указан запрещённый товар'});
assert.notEqual(nonMatching.summary, 'Слишком длинные слова в описании.');
assert.match(nonMatching.summary, /запрещённый товар/);
assert.match(nonMatching.nextStep, /Сверьте замечание/);

const unknown = present(JSON.stringify({code: 'FUTURE_REASON',
  description: 'Проверьте комплектность карточки'}));
assert.match(unknown.summary, /Проверьте комплектность карточки/);
assert.match(unknown.nextStep, /Сверьте замечание/);
assert.match(unknown.technical, /FUTURE_REASON/);

const malformed = present('{"error":{"code":');
assert.match(malformed.summary, /Площадка отклонила карточку/);
assert.equal(malformed.technical, '{"error":{"code":');

const longMessage = present({code: 'DESCRIPTION_DECLINE', description: 'x'.repeat(6000)});
assert.match(longMessage.summary, /Площадка отклонила карточку/);
assert.ok(longMessage.technical.length <= 5002);

const deep = {code: 'DESCRIPTION_DECLINE'};
let cursor = deep;
for (let index = 0; index < 8; index += 1) {
  cursor.child = {};
  cursor = cursor.child;
}
cursor.description = 'Описание содержит слишком длинные слова';
assert.notEqual(present(deep).summary, 'Слишком длинные слова в описании.');

const bareAllowedCode = present('DESCRIPTION_DECLINE');
assert.equal(bareAllowedCode.code, 'DESCRIPTION_DECLINE');
assert.notEqual(bareAllowedCode.summary, 'Слишком длинные слова в описании.');
""")


def test_listing_template_keeps_reason_safe_and_gallery_semantics_explicit():
    template = TEMPLATE.read_text(encoding="utf-8")
    status_partial = (ROOT / "templates/partials/listing_workspace_beta_header.html").read_text(encoding="utf-8")
    assert "{{ err.summary }}" in template
    assert "{{ err.nextStep }}" in template
    assert "{{ err.technical }}" in template
    assert 'v-html="err.' not in template
    assert ':alt="heroAlt"' in template
    assert 'width="320" height="427"' in template
    assert ':aria-pressed="heroSrc === url"' in template
    assert 'width="44" height="58"' in template
    assert "Открыть черновики Ozon" in template
    assert "v-text=\"providerBadge.hint || providerBadge.label\"" in status_partial
    assert "providerBadge.hint || listing.provider_status" not in status_partial
