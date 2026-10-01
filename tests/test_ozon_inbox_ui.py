"""Vue request lifecycle, local editor preservation, URL and uncertain POST gates."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).parents[1]


def run(body):
    node = shutil.which('node')
    if not node: pytest.skip('Node required for Vue behavioral checks')
    prelude = r'''
const assert=require('node:assert/strict');global.window=global;let handlers={},storage={};
global.document={hidden:false,getElementById:()=>null,activeElement:{focus(){}},addEventListener:(k,v)=>handlers[k]=v,removeEventListener:k=>delete handlers[k]};global.confirm=()=>true;
global.addEventListener=(name,f)=>handlers[name]=f;global.removeEventListener=name=>delete handlers[name];
global.sessionStorage={getItem:k=>storage[k]||null,setItem:(k,v)=>storage[k]=v};
global.mcatShared={imageDeadline:{}};global.location=new URL('https://fixture.test/marketplaces/reviews?account_id=1');
global.history={state:{},pushState:(s,_,url)=>location=new URL(url,location),replaceState:(s,_,url)=>location=new URL(url,location)};
'''
    source = (ROOT/'static/ozon-read-refresh.js').read_text() + (ROOT/'static/ozon-inbox.js').read_text()
    setup = r'''
const config={accountId:1,page:'/marketplaces/reviews',api:'/marketplaces/api/reviews',csrfToken:'synthetic'};
function make(){const options=ozonInbox.createOptions(config),page={...options.data(),...options.methods};
for(const [key,f]of Object.entries(options.computed))Object.defineProperty(page,key,{get:()=>f.call(page)});
page.$refs={detail:{open:false,showModal(){this.open=true},close(){this.open=false}},editor:{focus(){},select(){}}};return {page,options};}
function draft(id=10,text='Сохранённый ответ'){return {id,account_id:1,inbox_item_id:2,status:'draft',text,content_hash:'a'.repeat(64)};}
function item(patch={}){return {id:2,account_id:1,source_kind:'review',text:'Отзыв',reply_eligible:true,draft:draft(),...patch};}
function payload(patch={}){return {scope:{account_id:1,marketplace:'ozon'},filters:{source_kind:'review',search:'',status:'',listing_id:null},items:[item()],pagination:{page:1,total:1,pages:1},stats:{total:1,NEW:1,VIEWED:0,PROCESSED:0},capability:{available:true,account_ready:true},sync:null,...patch};}
function reply(data,patch={}){return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({success:true,data}),...patch};}
function state(patch={}){return {account_id:1,domain:'reviews',period:'90d',id:1,status:'pending',active:true,...patch};}
function editor(page){page.filters.itemId=2;page.selected=item();page.editor=page.savedText=page.selected.draft.text;}
'''
    script = prelude+source+setup+'\n(async()=>{'+body+'\n})().catch(e=>{console.error(e);process.exitCode=1});'
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_access_copy_does_not_assume_paid_plan_or_explain_provider_denial():
    template = (ROOT/'templates/marketplace_inbox.html').read_text()
    assert 'Проверьте доступ к методу в кабинете Ozon или уточните его у поддержки.' in template
    assert 'Причина доступа пока не подтверждена' in template
    assert 'Доступ можно перепроверить вручную; права и условия метода уточните в кабинете Ozon или у поддержки.' in template
    assert 'После изменения прав или тарифа' not in template
    assert 'Premium Plus' not in template
    assert 'может требоваться Premium' not in template


def test_late_and_foreign_response_never_replace_current_rows():
    run(r'''
const {page,options}=make(),requests=[];global.fetch=(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}));
const first=page.load();page.filters.search='новое';const latest=page.load();assert.equal(requests[0].opts.signal.aborted,true);
requests[1].resolve(reply(payload({filters:{...payload().filters,search:'новое'}})));await latest;
requests[0].resolve(reply(payload({items:[item({text:'old'})]})));await first;assert.equal(page.items[0].text,'Отзыв');assert.equal(page.observed.search,'новое');
const before=page.items;global.fetch=async()=>reply(payload({scope:{account_id:2,marketplace:'ozon'}}));await page.load();assert.equal(page.items,before);assert.ok(page.error);options.beforeUnmount.call(page);
''')


def test_uncertain_generation_survives_reopen_reload_and_only_changed_draft_resolves():
    run(r'''
let calls=0;const {page,options}=make();editor(page);global.fetch=async()=>{calls++;throw TypeError('offline')};
await page.writeDraft('ai');assert.equal(calls,1);assert.equal(page.uncertainDraft.previous,10);assert.equal(page.canDraft,false);await page.writeDraft('ai');assert.equal(calls,1);
page.closeDialog();await page.openItem(item());assert.equal(page.uncertainDraft.previous,10);
const next=make();editor(next.page);assert.equal(next.page.uncertainDraft.previous,10);
global.fetch=async()=>reply(item());await next.page.readItem();assert.ok(next.page.uncertainDraft);
global.fetch=async()=>reply(item({draft:draft(11,'Подготовленный новый ответ')}));await next.page.readItem();assert.equal(next.page.uncertainDraft,null);assert.equal(next.page.editor,'Подготовленный новый ответ');assert.equal(next.page.canDraft,true);
assert.equal(JSON.parse(storage['ozon-inbox-pending:1'])['2'],undefined);options.beforeUnmount.call(page);next.options.beforeUnmount.call(next.page);
''')


def test_lost_save_preserves_input_and_recovers_confirmed_version_without_retry():
    run(r'''
const {page,options}=make();editor(page);page.editor='Мой отредактированный ответ';let posts=0;
global.fetch=async()=>{posts++;throw TypeError('offline')};await page.writeDraft('save');assert.equal(page.editor,'Мой отредактированный ответ');assert.equal(page.uncertainDraft.mode,'save');
global.fetch=async()=>reply(item({draft:draft(12,'Мой отредактированный ответ')}));await page.readItem();assert.equal(page.uncertainDraft,null);assert.equal(page.dirty,false);assert.equal(posts,1);options.beforeUnmount.call(page);
''')


def test_conflict_and_detail_reload_keep_human_text_until_explicit_discard():
    run(r'''
const {page,options}=make();editor(page);page.editor='Несохранённые правки';let sent;
global.fetch=async(url,opts)=>{sent=JSON.parse(opts.body);return reply(null,{ok:false,status:409,json:async()=>({error:'Черновик изменился'})});};
await page.writeDraft('save');assert.equal(sent.draft_id,10);assert.equal(sent.expected_content_hash,'a'.repeat(64));assert.equal(page.editor,'Несохранённые правки');assert.equal(page.uncertainDraft,null);
global.fetch=async()=>reply(item({draft:draft(11,'Другая сохранённая версия')}));await page.readItem();assert.equal(page.editor,'Несохранённые правки');assert.equal(page.savedText,'Другая сохранённая версия');page.useSaved();assert.equal(page.dirty,false);options.beforeUnmount.call(page);
''')


def test_session_expiry_stops_reads_but_durable_provider_denial_does_not_expire_login():
    run(r'''
const {page,options}=make();global.fetch=async()=>reply(payload());await page.load();assert.equal(page.canSync,true);let calls=0;
global.fetch=async(url,opts)=>{calls++;return opts.method==='POST'?reply(state()):url.includes('/sync?')?reply(state({status:'failed',active:false,error_code:'inbox_access_denied'})):reply(payload({capability:{available:true,account_ready:true,live_access_denied:true}}));};
await page.syncNow();await page.loadRefreshStatus();assert.equal(page.sessionEnded,false);assert.equal(page.capability.live_access_denied,true);assert.equal(calls,3);
global.fetch=async()=>{calls++;return reply(null,{ok:false,status:401})};await page.load();let done=calls;await page.load();assert.equal(calls,done);assert.equal(page.sessionEnded,true);options.beforeUnmount.call(page);
''')


def test_sync_enqueues_once_and_recovers_durable_status_without_changing_editor():
    run(r'''
const {page,options}=make();global.fetch=async()=>reply(payload());await page.load();editor(page);page.editor='Мои правки';let sent=[];
global.fetch=async(url,opts)=>{if(opts.method==='POST'){sent.push(JSON.parse(opts.body));throw TypeError('offline');}return reply(payload());};
await page.syncNow();await page.syncNow();assert.equal(sent.length,1);assert.deepEqual(sent[0],{period:'90d',force:true});assert.ok(page.refreshUncertain);await page.load();assert.ok(page.refreshUncertain);
global.fetch=async()=>reply(state({id:null,status:'idle',active:false}));await page.loadRefreshStatus();assert.ok(page.refreshUncertain);
global.fetch=async(url)=>reply(url.includes('/sync?')?state({status:'completed',active:false}):payload());await page.loadRefreshStatus();assert.equal(page.refreshUncertain,false);assert.equal(page.editor,'Мои правки');options.beforeUnmount.call(page);
''')


def test_deep_link_filters_and_dialog_back_navigation():
    run(r'''
location=new URL('https://fixture.test/marketplaces/reviews?account_id=1&source_kind=question&status=NEW&search=SKU&page=2&item=2');
const {page,options}=make();assert.equal(page.filters.kind,'question');assert.equal(page.filters.page,2);assert.equal(page.searchDraft,'SKU');assert.equal(page.filters.itemId,2);
page.load=()=>{};page.navigate({status:'VIEWED',page:1});assert.equal(new URL(location).searchParams.get('status'),'VIEWED');assert.equal(new URL(location).searchParams.get('item'),null);assert.equal(new URL(location).searchParams.get('source_kind'),'question');options.beforeUnmount.call(page);
''')


def test_timeout_and_unmount_leave_rows_and_ignore_late_detail():
    run(r'''
const {page,options}=make();global.fetch=async()=>reply(payload());await page.load();const rows=page.items;const timers=new Map();let n=0;
global.setTimeout=(f,ms)=>{timers.set(++n,{f,ms});return n};global.clearTimeout=k=>timers.delete(k);
global.fetch=(_,opts)=>new Promise((_,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));
const loading=page.load();[...timers.values()].find(t=>t.ms===10000).f();await loading;assert.equal(page.items,rows);assert.ok(page.error.includes('вовремя'));
editor(page);let resolve;global.fetch=()=>new Promise(r=>resolve=r);const detail=page.readItem();options.beforeUnmount.call(page);resolve(reply(item({text:'late'})));await detail;assert.equal(page.selected.text,'Отзыв');
''')


def test_credentialed_or_script_images_are_rejected_and_dates_preserve_offsets():
    run(r'''
for(const value of ['javascript:alert(1)','https://user:pass@example.test/p.jpg',null])assert.equal(ozonInbox.imageURL(value),'');
assert.equal(ozonInbox.imageURL('https://example.test/photo.jpg'),'https://example.test/photo.jpg');assert.notEqual(ozonInbox.instant('2026-09-25T10:00:00Z'),'Дата не указана');assert.notEqual(ozonInbox.instant('2026-09-25T13:00:00+03:00'),'Дата не указана');
''')


def test_html_csrf_rejection_preserves_input_without_false_pending_marker():
    run(r'''
const {page,options}=make();editor(page);page.editor='Мои важные правки';
global.fetch=async()=>reply(null,{ok:false,status:400,headers:{get:()=> 'text/html'}});
await page.writeDraft('save');assert.equal(page.editor,'Мои важные правки');assert.equal(page.uncertainDraft,null);assert.ok(page.actionError.includes('обновите страницу'));
assert.equal(JSON.parse(storage['ozon-inbox-pending:1'])['2'],undefined);options.beforeUnmount.call(page);
''')


def test_kind_change_aborts_old_status_and_hidden_tab_resumes_only_get():
    run(r'''
const {page,options}=make(),requests=[];page.load=async()=>{};
global.fetch=(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}));
const init=page.initRefresh();requests[0].resolve(reply(state({id:null,status:'idle',active:false})));await init;
const old=page.loadRefreshStatus();page.changeKind('question');assert.equal(requests[1].opts.signal.aborted,true);
requests[2].resolve(reply(state({id:9,domain:'questions'})));await new Promise(r=>setImmediate(r));
requests[1].resolve(reply(state({status:'completed',active:false})));await old;assert.equal(page.refreshState.domain,'questions');assert.equal(page.refreshState.id,9);
document.hidden=true;handlers.visibilitychange();await page.loadRefreshStatus();assert.equal(requests.length,3);
document.hidden=false;handlers.visibilitychange();assert.ok(requests[3].url.includes('/questions/sync?'));assert.equal(requests[3].opts.method,undefined);
requests[3].resolve(reply(state({id:9,domain:'questions',status:'completed',active:false})));await new Promise(r=>setImmediate(r));assert.equal(page.refreshing,false);options.beforeUnmount.call(page);
''')


def test_foreign_refresh_status_never_clears_unknown_submit_and_csrf_400_is_rejected():
    run(r'''
const {page,options}=make();global.fetch=async()=>reply(payload());await page.load();
global.fetch=async()=>reply(state({account_id:999}));await page.syncNow();assert.ok(page.refreshUncertain);assert.equal(page.refreshState,null);
global.fetch=async()=>reply(state({domain:'questions'}));await page.loadRefreshStatus();assert.ok(page.refreshUncertain);assert.equal(page.refreshState,null);options.beforeUnmount.call(page);
const next=make();next.page.loaded=true;next.page.capability={available:true,account_ready:true};
global.fetch=async()=>({ok:false,status:400,json:async()=>{throw SyntaxError('html')}});await next.page.syncNow();assert.equal(next.page.refreshUncertain,false);assert.equal(next.page.refreshing,false);assert.ok(next.page.refreshStatusError);next.options.beforeUnmount.call(next.page);
''')
