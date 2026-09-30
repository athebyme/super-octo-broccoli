from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).parents[1]


def run(body):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    setup=r'''
const assert=require('node:assert/strict');global.window=global;let handlers={},timers=new Map(),next=0;
global.document={hidden:false,getElementById:()=>null,addEventListener:(k,v)=>handlers[k]=v,removeEventListener:k=>delete handlers[k]};
global.setTimeout=(f,ms)=>{timers.set(++next,{f,ms});return next;};global.clearTimeout=k=>timers.delete(k);
global.location=new URL('https://fixture.test/marketplaces/status?account_id=1');
'''
    fixture=r'''
const config={accountId:1,api:'/marketplaces/api/status'};
function make(){const options=ozonAccountHealth.options(config),page={...options.data(),...options.methods};return {page,options};}
function data(patch={}){return {scope:{marketplace:'ozon',account_id:1},account:{id:1},observation_only:true,publication_permission_evaluated:false,
 domains:['catalog','analytics','fulfillment','finance','reviews','questions'].map(domain=>({domain,freshness:'unknown',activity:'idle'})),operations:[],scheduler:{state:'healthy'},...patch};}
function reply(value,patch={}){return {ok:true,status:200,json:async()=>({success:true,data:value}),...patch};}
'''
    script=setup+(ROOT/'static/ozon-account-health.js').read_text()+fixture+'\n(async()=>{'+body+'\n})().catch(e=>{console.error(e);process.exitCode=1});'
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_read_only_lifecycle_old_response_hidden_tab_and_unmount():
    run(r'''
const {page,options}=make(),requests=[];global.fetch=(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}));
options.mounted.call(page);assert.equal(requests.length,1);assert.equal(requests[0].opts.method,undefined);
const newer=page.load();assert.ok(requests[0].opts.signal.aborted);requests[1].resolve(reply(data({observed_at:'new'})));await newer;
requests[0].resolve(reply(data({observed_at:'old'})));await new Promise(r=>setImmediate(r));assert.equal(page.data.observed_at,'new');
assert.equal([...timers.values()][0].ms,30000);document.hidden=true;handlers.visibilitychange();await page.load();assert.equal(requests.length,2);assert.equal(timers.size,0);
document.hidden=false;handlers.visibilitychange();assert.equal(requests.length,3);options.beforeUnmount.call(page);requests[2].resolve(reply(data({observed_at:'late'})));await new Promise(r=>setImmediate(r));assert.equal(page.data.observed_at,'new');assert.equal(timers.size,0);assert.equal(Object.keys(handlers).length,0);
''')


def test_wrong_scope_missing_domain_or_write_permission_never_replaces_observation():
    run(r'''
const {page,options}=make();global.fetch=async()=>reply(data());await page.load();const before=page.data;
for(const patch of [{scope:{marketplace:'ozon',account_id:2}},{domains:data().domains.slice(1)},{publication_permission_evaluated:true},{operations:[{id:'javascript:foo'}]}]){
 global.fetch=async()=>reply(data(patch));await page.load();assert.equal(page.data,before);assert.ok(page.error);
}
options.beforeUnmount.call(page);
''')


def test_timeout_network_and_session_failure_keep_last_observation():
    run(r'''
const {page,options}=make();global.fetch=async()=>reply(data());await page.load();const before=page.data;
global.fetch=async()=>{throw TypeError('offline')};await page.load();assert.equal(page.data,before);assert.ok(page.error.includes('соединения'));
global.fetch=(_,opts)=>new Promise((_,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('abort','AbortError'))));
const pending=page.load();[...timers.values()].find(t=>t.ms===10000).f();await pending;assert.equal(page.data,before);assert.ok(page.error.includes('слишком'));
let count=0;global.fetch=async()=>{count++;return reply(null,{ok:false,status:401})};await page.load();await page.load();assert.equal(count,1);assert.equal(page.sessionEnded,true);assert.equal(timers.size,0);options.beforeUnmount.call(page);
''')


def test_no_false_zero_unknown_age_and_owned_links():
    run(r'''
const {page,options}=make();assert.equal(ozonAccountHealth.age(null),'Время неизвестно');assert.equal(ozonAccountHealth.age(0),'меньше минуты');
assert.equal(page.domainUrl({domain:'questions'}),'/marketplaces/reviews?account_id=1&source_kind=question');assert.equal(page.operationUrl({id:42}),'/marketplaces/operations/42');
assert.ok(page.message({activity:'waiting',last_error_code:'account_busy'}).includes('другая задача'));assert.ok(!page.message({activity:'waiting',last_error_code:'account_busy'}).includes('Ozon ограничил'));options.beforeUnmount.call(page);
''')
