"""Vue draft workspace: explicit identity, tenant selection and single writes."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).parents[1]
SETUP = r'''
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');global.window=global;
global.document={getElementById:()=>null};vm.runInThisContext(fs.readFileSync('./static/draft-source-picker.js','utf8'));
const saved=new Map();global.sessionStorage={getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value),removeItem:key=>saved.delete(key)};
require('./static/ozon-drafts-vue.js');
const account=id=>({id,label:'Магазин '+id,can_publish:true});
const row=(id,account_id=1)=>({id,account_id,version:3,status:'ready',validation_status:'valid',validated_at:'2026-07-25T12:00:00',title:'Товар '+id,validation_summary:{publishable:true}});
const config={enabled:true,publicationEnabled:true,accounts:[account(1),account(2)],rows:[row(1),{...row(2),published_listing_id:42},row(3,2)],filters:{account_id:2},pagination:{page:1,per_page:50},sourceSearch:{items:[]},urls:{sources:'/sources',create:'/create',review:'/review',editorBase:'/editor/',runBase:'/runs/',repairDraftSelection:'/selection/repair'},csrf:'synthetic'};
const definition=ozonDraftsVue.createOptions(config);const page=definition.data();
for(const [key,value]of Object.entries(definition.methods))page[key]=value.bind(page);
for(const [key,value]of Object.entries(definition.computed))Object.defineProperty(page,key,{get:value.bind(page)});
page.$nextTick=fn=>fn();page.$refs={sourceQuery:{setCustomValidity(){},focus(){},reportValidity(){}}};
'''


def run_node(source):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for Vue behavior checks')
    result = subprocess.run([node, '-e', SETUP + source], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_bulk_selection_requires_one_ready_account_and_current_access():
    run_node(r'''
assert.equal(page.createAccountId,2);assert.equal(page.bulkReady,false);
page.toggleRow(page.rows[0],true);page.toggleRow(page.rows[2],true);assert.deepEqual(page.selectedDrafts,[1]);
page.toggleRow(page.rows[1],true);assert.equal(page.bulkReady,true);
assert.deepEqual(JSON.parse(saved.get('ozon-drafts-selection-v1')),{account_id:1,draft_ids:[1,2]});
page.accounts[0].credential_expires_at='2020-01-01T00:00:00';assert.equal(page.bulkReady,true,'Fresh GET review owns the final account check');
page.clearSelection();assert.equal(page.selectedAccountId,null);page.toggleRow(page.rows[2],true);assert.deepEqual(page.selectedDrafts,[3]);
assert.equal(page.imageUrl({id:7,primary_image:'javascript:alert(1)'}),'');assert.equal(page.imageUrl({id:7,primary_image:'//evil.test/image'}),'');
assert.equal(page.imageUrl({id:7,primary_image:'/api/photos/imported-product/13/0'}),'/api/photos/imported-product/13/0');
''')


def test_ready_list_rows_are_labeled_as_saved_history_with_a_fresh_review_step():
    run_node(r'''
const savedReady={...row(12),validation_summary:{publishable:true,error_count:0}};
assert.equal(page.stateLabel(savedReady),'Готово по сохранённым данным');
assert.equal(page.stateTone(savedReady),'muted');
assert.equal(page.validationLabel(savedReady),'Проверка сохранена 25.07.2026');
assert.equal(page.eligible(savedReady),true,'selection remains preliminary and review owns current validation');
const missingDate={...savedReady,validated_at:null};assert.equal(page.validationLabel(missingDate),'Проверка сохранена');
''')
    template = (ROOT/'templates/marketplace_drafts.html').read_text()
    assert "('ready','Сохранённый статус готовности')" in template
    assert 'Проверьте текущие поля перед отправкой.' in template
    assert "'Открыть карточку для проверки →'" in template


def test_bulk_selection_crosses_pages_and_navigates_to_read_only_review():
    run_node(r'''
page.toggleRow(page.rows[0],true);page.toggleRow(page.rows[1],true);
const another={...config,rows:[row(4),row(5)]};
const other=ozonDraftsVue.createOptions(another).data();
assert.deepEqual(other.selectedDrafts,[1,2]);assert.equal(other.selectedAccountId,1);
let target;global.location={assign:url=>target=url};page.reviewBulk();
assert.equal(target,'/review?account_id=1&draft_ids=1%2C2');
assert(!saved.get('ozon-upload-review-v1'),'No publication POST or consent is inferred from list selection');
page.clearSelection();assert.equal(saved.has('ozon-drafts-selection-v1'),false);
''')


def test_edit_selection_stays_exact_across_pages_filter_changes_and_return():
    run_node(r'''
page.setSelectionMode('edit');page.toggleEditDraft(page.rows[0],true);
assert.deepEqual(page.editSelectedDrafts,[1]);assert.equal(page.editSelectedAccountId,1);
assert.deepEqual(JSON.parse(saved.get('ozon-drafts-edit-selection-v1')),{account_id:1,draft_ids:[1]});
const pageTwoOptions=ozonDraftsVue.createOptions({...config,rows:[row(4),row(5,2)],filters:{account_id:1,status:'ready',page:2,per_page:1}});
const pageTwo=pageTwoOptions.data();
assert.equal(pageTwo.selectionMode,'edit','page navigation restores edit mode without another click');
assert.deepEqual(pageTwo.editSelectedDrafts,[1]);assert.equal(pageTwo.editSelectedAccountId,1);
for(const [key,value]of Object.entries(pageTwoOptions.methods))pageTwo[key]=value.bind(pageTwo);
for(const [key,value]of Object.entries(pageTwoOptions.computed))Object.defineProperty(pageTwo,key,{get:value.bind(pageTwo)});
pageTwo.toggleEditDraft(pageTwo.config.rows[0],true);
pageTwo.toggleEditDraft(pageTwo.config.rows[1],true);
assert.deepEqual(pageTwo.editSelectedDrafts,[1,4],'a different-account row cannot widen the exact selection');
global.location={origin:'https://app.test',assign:url=>{global.target=url;}};
assert.equal(pageTwo.startEditSelection(),true);
assert.equal(global.target,'/selection/repair?account_id=1&draft_ids=1%2C4&return_page=2&return_per_page=1&return_status=ready&return_account_id=1');
const returned=ozonDraftsVue.createOptions({...config,filters:{account_id:null,status:null,page:1,per_page:1}}).data();
assert.equal(returned.selectionMode,'edit','the editor return restores the edit selection mode');
assert.deepEqual(returned.editSelectedDrafts,[1,4]);assert.equal(returned.editSelectedAccountId,1);
''')
    template = (ROOT/'templates/marketplace_drafts.html').read_text()
    assert 'data-draft-selection-mode' in template
    assert 'data-draft-selection-start' in template
    assert 'data-draft-selection-check' in template
    assert 'data-draft-selection-page' in template
    assert 'per_page=filters.per_page' in template


def test_edit_select_page_requires_one_account_and_never_truncates_at_limit():
    run_node(r'''
const bind=options=>{const value=options.data();for(const [key,fn]of Object.entries(options.methods))value[key]=fn.bind(value);
for(const [key,fn]of Object.entries(options.computed))Object.defineProperty(value,key,{get:fn.bind(value)});return value;};
saved.delete('ozon-drafts-edit-selection-v1');
let current=bind(ozonDraftsVue.createOptions({...config,filters:{account_id:1},rows:[row(11),row(12),{...row(13),status:'archived'}]}));
assert.equal(current.editPageAccountId,1);assert.equal(current.editPageCandidates.length,2);
assert.equal(current.selectEditPage(),true);assert.deepEqual(current.editSelectedDrafts,[11,12]);
current=bind(ozonDraftsVue.createOptions({...config,rows:[row(21,1),row(22,2)]}));
assert.equal(current.editPageAccountId,null,'mixed-account pages hide the page action');
assert.equal(current.selectEditPage(),false);
saved.set('ozon-drafts-edit-selection-v1',JSON.stringify({account_id:1,draft_ids:[31]}));
current=bind(ozonDraftsVue.createOptions({...config,rows:[row(32,2)]}));
assert.equal(current.selectEditPage(),false);assert.deepEqual(current.editSelectedDrafts,[31]);
const many=Array.from({length:200},(_,index)=>row(1000+index,1));
current=bind(ozonDraftsVue.createOptions({...config,rows:many}));
assert.equal(current.selectEditPage(),false,'the page action does not truncate a selection over 200');
assert.match(current.editSelectionError,/200/);assert.deepEqual(current.editSelectedDrafts,[31]);
''')


def test_edit_selection_filter_return_omits_empty_status_and_all_account():
    run_node(r'''
page.setSelectionMode('edit');page.toggleEditDraft(page.rows[0],true);
let target;global.location={origin:'https://app.test',assign:url=>{target=url;}};
page.config.filters={account_id:null,status:null,page:3,per_page:2};
assert.equal(page.startEditSelection(),true);
const parsed=new URL(target,'https://app.test');
assert.equal(parsed.searchParams.get('account_id'),'1');
assert.equal(parsed.searchParams.get('draft_ids'),'1');
assert.equal(parsed.searchParams.get('return_page'),'3');
assert.equal(parsed.searchParams.get('return_per_page'),'2');
assert.equal(parsed.searchParams.has('return_status'),false);
assert.equal(parsed.searchParams.has('return_account_id'),false);
''')


def test_source_identity_requires_selection_and_late_results_do_not_replace_it():
    run_node(r'''
(async()=>{
const pending=[];global.fetch=(url,options)=>new Promise(resolve=>pending.push({url,options,resolve}));
page.query='старый';page.changed();await new Promise(r=>setTimeout(r,270));
page.query='новый';page.changed();await new Promise(r=>setTimeout(r,270));assert.equal(pending[0].options.signal.aborted,true);
const response=items=>({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({items})});
pending[1].resolve(response([{id:25,title:'Новый товар'}]));await new Promise(setImmediate);page.choose(page.items[0]);
pending[0].resolve(response([{id:15,title:'Старый товар'}]));await new Promise(setImmediate);assert.equal(page.selectedId,'25');assert.equal(page.query,'Новый товар');assert.equal(page.open,false);
let calls=[];let target;global.location={assign:url=>target=url};page.write=async(url,body)=>{calls.push(body);return {draft:{id:99}}};
await page.createDraft({preventDefault(){}});await page.createDraft({preventDefault(){}});assert.equal(calls.length,1);assert.equal(calls[0].imported_product_id,25);assert.equal(calls[0].account_id,2);assert.equal(target,'/editor/99');
page.destroy();
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_failed_draft_creation_moves_focus_to_alert_after_busy_state_renders():
    run_node(r'''
(async()=>{
page.selectedId='25';page.createAccountId=2;
let focused=0, tickResolved=false, busyAtFocus;
page.$nextTick=callback=>Promise.resolve().then(()=>{tickResolved=true;callback();});
page.$refs.actionError={focus(){focused++;busyAtFocus=page.busy;}};
page.write=async()=>{await Promise.resolve();throw Object.assign(Error('Не удалось создать черновик'),{status:400});};
await page.createDraft({preventDefault(){}});
assert.equal(focused,1,'the newly rendered alert receives keyboard focus');
assert.equal(tickResolved,true,'focus waits for Vue’s render tick');
assert.equal(busyAtFocus,'','the disabled submit fieldset is released before focus moves');
assert.match(page.actionError,/Не удалось создать черновик/);
assert.equal(page.actionUncertain,false);
})().catch(error=>{console.error(error);process.exitCode=1});
''')




def test_source_search_handles_expired_session_without_parsing_html():
    run_node(r'''
(async()=>{
global.fetch=async()=>({status:200,redirected:true,json:()=>{throw Error('Must not parse login HTML')}});
await page.search();assert.match(page.error,/Сессия истекла/);assert.equal(page.loading,false);page.destroy();
})().catch(error=>{console.error(error);process.exitCode=1});
''')
