"""Seller AI completion outcomes stay distinct and actionable in the Vue UI."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SETUP = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
global.window = global;
let runDefinition;
const run = {job_uid:'ozon-ai-'+'a'.repeat(32), account_id:7, mode:'draft_suggestions',
    status:'completed', total:3, items:[], summary:{rejection_reasons:{invalid_evidence_path:1,unclassified:1}}};
const config = {run, csrf_token:'synthetic', urls:{editorBase:'/drafts/'}};
global.document = {getElementById:id => id === 'oai-run-bootstrap' ? {textContent:JSON.stringify(config)} : null,
    hidden:false, addEventListener(){}, removeEventListener(){}};
global.Vue = {createApp(definition){runDefinition=definition; return {mount(){}};}};
require('./static/ozon-draft-ai-run.js');
const runMethods = runDefinition.methods;
const runComputed = runDefinition.computed;
require('./static/ozon-draft-ai-review.js');
const reviewComponent = global.ozonDraftAIReview;
'''


def run_node(source):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for Vue behavior checks')
    result = subprocess.run(
        [node, '-e', SETUP + source], cwd=ROOT, capture_output=True,
        text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_run_page_distinguishes_absent_evidence_rejected_suggestions_and_unknown_response():
    run_node(r'''
const absent = {status:'no_evidence', code:null};
const rejected = {status:'no_evidence', code:'ai_fields_rejected'};
const unknown = {status:'unknown_response', code:'unknown_response'};
const absentText = runMethods.itemGuidance(absent);
const rejectedText = runMethods.itemGuidance(rejected);
const unknownText = runMethods.itemGuidance(unknown);
assert.match(absentText,/Модель не вернула подтверждённых предложений/);
assert.match(absentText,/вручную/);
assert.match(rejectedText,/отклонила/);
assert.match(rejectedText,/ничего не сохранено/);
assert.match(rejectedText,/новый поиск/);
assert.match(unknownText,/не подтверждён/);
assert.match(unknownText,/автоматического повтора нет/);
assert.match(unknownText,/историю запуска/);
for (const text of [absentText,rejectedText,unknownText,runMethods.itemCode('invalid_evidence_path')]) {
    assert(!/invalid_evidence_path|ai_fields_rejected/.test(text));
}
assert.equal(runComputed.rejectedCount.call({run}),2);
assert.match(runMethods.rejectionSummary.call({rejectedCount:2}),/2 карточках/);
const outcomeSummary = runComputed.outcomeSummary.call({run, rejectedCount:2,
    rejectionSummary:()=> 'Проверка источника отклонила AI-предложения.'});
assert.match(outcomeSummary,/новый поиск явно/);
assert(!/invalid_evidence_path|ai_fields_rejected/.test(outcomeSummary));
assert.equal(runMethods.itemGuidance({status:'proposed',code:'ai_fields_rejected'}).includes('Часть'),true);
''')


def test_card_review_repeats_safe_outcome_guidance_without_raw_reasons():
    run_node(r'''
const methods = reviewComponent.component.methods;
const message = (status,code,suggestions=[]) => methods.itemOutcomeMessage.call({doc:{
    item:{status,code}, suggestions
}});
const absent = message('no_evidence',null);
const rejected = message('no_evidence','ai_fields_rejected');
const partial = message('proposed','ai_fields_rejected',[{id:1}]);
const unknown = message('unknown_response','unknown_response');
assert.match(absent,/Модель не вернула подтверждённых предложений/);
assert.match(absent,/вручную/);
assert.match(rejected,/отклонила/);
assert.match(rejected,/ничего не сохранено/);
assert.match(partial,/Часть AI-предложений/);
assert.match(unknown,/не подтверждён/);
assert.match(unknown,/автоматического повтора нет/);
for (const text of [absent,rejected,partial,unknown,methods.issueLabel('ai_fields_rejected')]) {
    assert(!/invalid_evidence_path|ai_fields_rejected/.test(text));
}
''')
