from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).parents[1]


def run(body):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    setup=r'''
const assert=require('node:assert/strict');global.window=global;const handlers={},timers=new Map(),storage=new Map();let seq=0;
global.document={hidden:false,getElementById:()=>null,addEventListener:(k,v)=>handlers[k]=v,removeEventListener:k=>delete handlers[k]};
global.addEventListener=(k,v)=>handlers[k]=v;global.removeEventListener=k=>delete handlers[k];
global.setTimeout=(f,ms)=>{timers.set(++seq,{f,ms});return seq};global.clearTimeout=k=>timers.delete(k);
global.location=new URL('https://fixture.test/marketplaces/quality?account_id=1');
global.history={pushState(s,t,url){location=new URL(url,location)},replaceState(s,t,url){location=new URL(url,location)}};
global.sessionStorage={getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)};
'''
    fixture=r'''
const config={accountId:1,page:'/marketplaces/quality',api:'/marketplaces/api/quality/workspace',refresh:'/marketplaces/api/quality/refresh',csrfToken:'synthetic-token'};
function make(){const options=ozonQuality.options(config),page={...options.data(),...options.methods,$refs:{dialog:{open:false,showModal(){this.open=true},close(){this.open=false}}},$nextTick:async()=>{}};for(const [key,f] of Object.entries(options.computed))Object.defineProperty(page,key,{get:()=>f.call(page)});return {options,page};}
function item(n=1,patch={}){return {listing_id:n,account_id:1,marketplace_code:'ozon',entity_kind:'marketplace_listing',state:'observed',score:0,reasons:[],url:`/marketplaces/listings/view/${n}?account_id=1`,...patch};}
function data(patch={}){return {scope:{marketplace_code:'ozon',account_id:1},items:[item()],summary:{total:1,assessed:1,reasons:[]},filters:{search:'',severity:'',reason:'',state:'',sort_by:'priority',sort_dir:'desc'},pagination:{page:1,per_page:25,total:1,pages:1},...patch};}
function reply(data,patch={}){return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({success:true,data}),...patch};}
function job(status='pending'){return {job_uid:'oq:1:abc',account_id:1,marketplace_code:'ozon',status,active:['pending','running'].includes(status),processed:status==='completed'?1:0};}
function jobReply(value){return reply(null,{json:async()=>({success:true,account_id:1,marketplace_code:'ozon',job:value})});}
'''
    script=setup+(ROOT/'static/ozon-quality.js').read_text()+fixture+'\n(async()=>{'+body+'\n})().then(()=>console.log("DONE")).catch(e=>{console.error(e);process.exitCode=1});'
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert 'DONE' in result.stdout,result.stderr


def test_late_list_result_and_unmount_preserve_current_selection():
    run(r'''
const {page,options}=make(),requests=[];global.fetch=(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}));
const old=page.load();page.filters.search='новое';const next=page.load();assert.ok(requests[0].opts.signal.aborted);
requests[1].resolve(reply(data({filters:{...data().filters,search:'новое'},items:[item(2)]})));await next;
requests[0].resolve(reply(data()));await old;assert.equal(page.items[0].listing_id,2);assert.equal(page.observedFilters.search,'новое');
const pending=page.load();options.beforeUnmount.call(page);requests[2].resolve(reply(data()));await pending;assert.equal(page.items[0].listing_id,2);assert.equal(timers.size,0);
''')


def test_wrong_scope_filter_pagination_or_item_does_not_replace_last_good():
    run(r'''
const {page}=make();global.fetch=async()=>reply(data());await page.load();const saved=page.items;
for(const patch of [{scope:{account_id:2,marketplace_code:'ozon'}},{items:[item(1,{account_id:2})]},{items:[item(1,{url:'https://outside.test/'})]},{items:[item(),item()]},{filters:{...data().filters,search:'other'}},{pagination:{page:2,per_page:25,total:1,pages:1}}]){global.fetch=async()=>reply(data(patch));await page.load();assert.equal(page.items,saved);assert.ok(page.error);}
page.filters.page=2;assert.ok(page.stale);
''')


def test_detail_late_response_close_and_exact_url():
    run(r'''
const {page}=make(),requests=[];global.fetch=(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}));
const first=page.openDetail(1);await Promise.resolve();const second=page.openDetail(2);await Promise.resolve();assert.ok(requests[0].opts.signal.aborted);
requests[1].resolve(reply({...item(2),breakdown:{},metrics:[]}));await second;requests[0].resolve(reply({...item(),breakdown:{},metrics:[]}));await first;assert.equal(page.detail.listing_id,2);assert.equal(location.search,'?account_id=1&listing_id=2');
const pending=page.loadDetail();page.closeDetail();requests[2].resolve(reply({...item(2),breakdown:{},metrics:[]}));await pending;assert.equal(page.detail,null);assert.equal(page.$refs.dialog.open,false);assert.equal(location.search,'?account_id=1');
''')


def test_lost_post_blocks_repetition_until_get_and_completion_does_not_reset_list():
    run(r'''
const {page,options}=make();let calls=[];page.items=[item(8)];global.fetch=async(url,opts)=>{calls.push({url,opts});throw new TypeError('offline');};
await page.startRefresh();assert.equal(calls.length,1);assert.equal(calls[0].opts.method,'POST');assert.equal(calls[0].opts.headers['X-CSRFToken'],'synthetic-token');assert.equal(calls[0].opts.body,'{}');assert.ok(page.jobUncertain);assert.equal(page.canRefresh,false);
await page.startRefresh();assert.equal(calls.length,1);
global.fetch=async(url,opts)=>{calls.push({url,opts});return jobReply(job());};await page.loadJob();assert.equal(calls[1].opts.method,'GET');assert.equal(page.jobUncertain,false);assert.equal(page.canRefresh,false);
global.fetch=async()=>jobReply(job('completed'));await page.loadJob();assert.ok(page.jobCompleted);assert.equal(page.items[0].listing_id,8);assert.equal(page.canRefresh,true);
options.beforeUnmount.call(page);assert.equal(timers.size,0);
''')


def test_job_scope_error_and_hidden_page_do_not_poll_or_clear_uncertainty():
    run(r'''
const {page,options}=make();page.jobUncertain=true;global.fetch=async()=>jobReply({...job(),account_id:2});await page.loadJob();assert.ok(page.jobError);assert.ok(page.jobUncertain);
document.hidden=true;page.scheduleJob();assert.equal(timers.size,0);document.hidden=false;page.scheduleJob();assert.ok([...timers.values()].some(t=>t.ms===15000));options.beforeUnmount.call(page);assert.equal(timers.size,0);
''')


def test_selection_across_pages_storage_scope_cap_and_ai_context():
    run(r'''
const {page}=make();global.fetch=async()=>reply(data());page.items=[item(1),item(2)];page.togglePage();assert.deepEqual(page.selected,[1,2]);page.items=[item(3)];page.togglePage();assert.deepEqual(page.selected,[1,2,3]);
const reloaded=make().page;reloaded.restoreSelection();assert.deepEqual(reloaded.selected,[1,2,3]);reloaded.saveCollection();const context=JSON.parse(storage.get('seller_hub_marketplace_collection'));assert.deepEqual(context,{entity_kind:'marketplace_listing',marketplace_code:'ozon',account_id:1,listing_ids:[1,2,3]});
page.selected=Array.from({length:200},(_,i)=>i+1);page.toggle(201);assert.equal(page.selected.length,200);assert.ok(page.selectionMessage);page.navigate({search:'новое',page:1});assert.equal(page.selected.length,0);
''')


def test_timeout_retains_list_and_session_expiry_stops_all_new_requests():
    run(r'''
const {page,options}=make();global.fetch=async()=>reply(data());await page.load();const saved=page.items;
global.fetch=(url,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Object.assign(Error(),{name:'AbortError'}))));const slow=page.load();[...timers.values()].find(t=>t.ms===10000).f();await slow;assert.match(page.error,/слишком много времени/);assert.equal(page.items,saved);
let calls=0;global.fetch=async()=>{calls++;return reply(null,{ok:false,status:401})};await page.load();assert.ok(page.sessionEnded);await page.load();await page.loadJob();await page.startRefresh();await page.openDetail(1);assert.equal(calls,1);assert.equal(page.items,saved);options.beforeUnmount.call(page);assert.equal(timers.size,0);
''')


def test_template_has_vue_accessible_dialog_and_no_raw_json_or_browser_recompute_loop():
    template=(ROOT/'templates/marketplace_quality.html').read_text()
    assert 'x-data=' not in template and 'x-ignore' in template and '<dialog' in template
    assert 'JSON.stringify' not in template and 'ozonQualityPage' not in template
    assert 'aria-labelledby="oqw-detail-title"' in template and 'width="56" height="70"' in template
    assert 'v-image-deadline' in template and 'listing_ids' not in template
