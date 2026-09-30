from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).parents[1]


def run(body):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    setup=r'''
const assert=require('node:assert/strict');global.window=global;const handlers={},timers=new Map();let seq=0;
global.document={getElementById:()=>null};global.addEventListener=(k,v)=>handlers[k]=v;global.removeEventListener=k=>delete handlers[k];
global.setTimeout=(f,ms)=>{timers.set(++seq,{f,ms});return seq};global.clearTimeout=k=>timers.delete(k);
global.location=new URL('https://fixture.test/marketplaces/finance/changes?account_id=1&older=10&newer=11');
global.history={pushState(s,t,url){location=new URL(url,location)},replaceState(s,t,url){location=new URL(url,location)}};
'''
    fixture=r'''
const config={accountId:1,page:'/marketplaces/finance/changes',api:'/marketplaces/api/finance/changes',historyApi:'/marketplaces/api/finance/history'};
function make(){const options=ozonFinanceChanges.options(config),page={...options.data(),...options.methods};for(const [key,f] of Object.entries(options.computed))Object.defineProperty(page,key,{get:()=>f.call(page)});return {options,page};}
function data(patch={}){return {scope:{marketplace:'ozon',account_id:1},observation_only:true,accounting_reconciliation:false,older:{id:10,period:'30d',end:'2026-09-24'},newer:{id:11,period:'30d',end:'2026-09-25'},period:{start:'2026-08-27',end:'2026-09-24',common_dates_only:true},filter:'',counts:{added:0,missing:0,changed:0,unchanged:1},items:[],totals:[],pagination:{page:1,per_page:50,total:0,pages:0},...patch};}
function reply(value,patch={}){return {ok:true,status:200,json:async()=>({success:true,data:value}),...patch};}
'''
    source='\n'.join((ROOT/'static'/p).read_text() for p in ('ozon-finance.js','ozon-finance-changes.js'))
    script=setup+source+fixture+'\n(async()=>{'+body+'\n})().then(()=>console.log("DONE")).catch(e=>{console.error(e);process.exitCode=1});'
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert 'DONE' in result.stdout,result.stderr


def test_late_responses_and_unmount_never_replace_selected_pair():
    run(r'''
const {page,options}=make(),requests=[];global.fetch=(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}));
const first=page.load();page.selection.newer='12';const second=page.load();assert.ok(requests[0].opts.signal.aborted);
assert.equal(requests[1].opts.method,undefined);assert.equal(requests[1].opts.cache,'no-store');assert.ok(requests[1].url.includes('newer=12'));
requests[1].resolve(reply(data({newer:{id:12}})));await second;requests[0].resolve(reply(data()));await first;assert.equal(page.data.newer.id,12);
const pending=page.load();options.beforeUnmount.call(page);requests[2].resolve(reply(data()));await pending;assert.equal(page.data.newer.id,12);assert.equal(timers.size,0);
''')


def test_scope_contract_and_pair_mismatch_preserve_previous_result():
    run(r'''
const {page}=make();global.fetch=async()=>reply(data());await page.load();const saved=page.data;
for(const patch of [{scope:{marketplace:'ozon',account_id:2}},{newer:{id:12}},{accounting_reconciliation:true},{observation_only:false},{filter:'added'},{pagination:{page:2,per_page:50,total:0,pages:0}},{items:[{kind:'refund',fields:[]}]}]){global.fetch=async()=>reply(data(patch));await page.load();assert.equal(page.data,saved);assert.ok(page.error);}
page.selection.older='9';assert.equal(page.stale,true);let count=0;global.fetch=async()=>{count++;return reply(data())};page.filter('changed');assert.equal(count,0);
const link=new URL(page.factUrl('older',{id:7}),'https://fixture.test');assert.equal(link.searchParams.get('snapshot_id'),'10');assert.equal(link.searchParams.get('fact_id'),'7');assert.equal(link.searchParams.get('account_id'),'1');
''')


def test_network_timeout_expired_session_and_no_automatic_provider_work():
    run(r'''
const {page}=make();global.fetch=async()=>reply(data());await page.load();const saved=page.data;
global.fetch=async()=>{throw new TypeError('offline')};await page.load();assert.equal(page.data,saved);assert.match(page.error,/Нет соединения/);
global.fetch=(url,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Object.assign(Error(),{name:'AbortError'}))));const pending=page.load();const timer=[...timers.values()][0];assert.equal(timer.ms,10000);timer.f();await pending;assert.match(page.error,/слишком много времени/);assert.equal(page.data,saved);
let calls=0;global.fetch=async()=>{calls++;return reply(null,{ok:false,status:401})};await page.load();assert.ok(page.sessionEnded);await page.load();await page.loadHistory();assert.equal(calls,1);assert.equal(page.data,saved);assert.equal(timers.size,0);
''')


def test_pruned_anchor_does_not_fall_back_to_latest_and_history_is_exact():
    run(r'''
const {page}=make();let urls=[];global.fetch=async url=>{urls.push(url);return {ok:false,status:404,json:async()=>({error:'Эта загрузка недоступна'})}};await page.loadHistory();assert.equal(page.selection.newer,'11');assert.equal(urls.length,1);assert.ok(urls[0].includes('anchor_id=11'));assert.match(page.error,/недоступна/);assert.equal(page.data,null);
global.fetch=async url=>{if(url.includes('/history'))return reply({scope:{marketplace:'ozon',account_id:1},observation_only:true,items:[{id:11},{id:10}],anchor:{id:12}});throw Error('must not compare')};await page.loadHistory();assert.match(page.error,/История загрузок не подтверждена/);
''')
