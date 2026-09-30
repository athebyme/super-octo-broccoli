"""Finance amounts, exact read scope, stale response and URL lifecycle."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).parents[1]
SETUP=r'''
const assert=require('node:assert/strict');global.window=global;
global.location=new URL('https://fixture.test/marketplaces/finance?account_id=1&search=abc&type_id=7&sign=negative&page=2');
global.history={state:{},pushState(state,_,url){this.state=state;location=new URL(url,location)},replaceState(state,_,url){this.pushState(state,_,url)},back(){this.backCalled=true}};
const listeners=new Map();global.addEventListener=(k,v)=>listeners.set(k,v);global.removeEventListener=k=>listeners.delete(k);
global.document={hidden:false,getElementById:()=>null,body:{style:{overflow:''}},activeElement:{isConnected:true,focus(){}},addEventListener:window.addEventListener,removeEventListener:window.removeEventListener};
global.mcatShared={imageDeadline:{}};
'''


def run(scenario):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    scripts='\n'.join((ROOT/'static'/p).read_text() for p in ['ozon-read-refresh.js','ozon-finance.js'])
    script=SETUP+scripts+'\n(async()=>{\n'+scenario+"\n})().then(()=>console.log('SCENARIO_COMPLETED')).catch(e=>{console.error(e);process.exitCode=1});"
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert 'SCENARIO_COMPLETED' in result.stdout,result.stderr


def test_amounts_preserve_signed_decimal_precision_and_unknown_currency():
    run(r'''
const normalize=s=>s.replace(/[\s\u00a0\u202f]/g,'');
assert.equal(normalize(ozonFinance.money('9999999999999999.1234','RUB')),'9999999999999999,1234₽');
assert.equal(normalize(ozonFinance.money('-0.0001','USD')),'−0,0001$');
assert.equal(normalize(ozonFinance.money(0,'RUB')),'0,00₽');
assert.equal(normalize(ozonFinance.money('-0.0000','RUB')),'0,00₽');
assert.ok(ozonFinance.money('12',null).includes('валюта не указана'));
for(const v of [null,undefined,'',true,[],[1],{},'NaN','1e20','12.00001','one'])assert.equal(ozonFinance.money(v,'RUB'),'Сумма не указана');
''')


def test_finance_list_detail_races_url_scope_and_session_end():
    run(r'''
const config={accountId:1,csrfToken:'test',api:'/api/finance',page:'/marketplaces/finance'};
const options=ozonFinance.createOptions(config);
const page={...options.data(),...options.methods,$nextTick:async()=>{},$refs:{drawer:{open:false,showModal(){this.open=true},close(){this.open=false}}}};
for(const [k,f] of Object.entries(options.computed))Object.defineProperty(page,k,{get:()=>f.call(page)});
assert.equal(page.filters.page,2);assert.equal(page.filters.typeId,'7');assert.equal(page.filters.sign,'negative');
let requests=[];global.fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
const body=(id,p=1,aid=1)=>({items:[{id}],totals:[{currency:'RUB',net:'-10.0000'}],pagination:{page:p,per_page:25,total:30,pages:2},scope:{account_id:aid}});
const reply=(n,data,status=200)=>requests[n].resolve({ok:status===200,status,headers:{get:()=> 'application/json'},json:async()=>({success:status===200,data})});
const old=page.load();page.filters={...page.filters,period:'7d',page:1};page.period='7d';const fresh=page.load();assert.equal(requests[0].options.signal.aborted,true);
reply(1,body(7));await fresh;reply(0,body(30,2));await old;assert.equal(page.items[0].id,7);assert.equal(page.observedFilters.period,'7d');
const bad=page.load();reply(2,body(99,1,2));await bad;assert.equal(page.items[0].id,7);assert.ok(page.error.includes('магазина'));
const invalid=page.load();reply(3,{...body(99),pagination:{page:1,per_page:25,total:30,pages:99}});await invalid;assert.equal(page.items[0].id,7);
page.searchDraft=' _100% ';page.applyFilters();assert.equal(new URL(location).searchParams.get('search'),'_100%');assert.equal(new URL(location).searchParams.get('type_id'),'7');reply(4,body(8));await new Promise(setImmediate);
const a=page.openDetail(11);await new Promise(setImmediate);const b=page.openDetail(12);await new Promise(setImmediate);
const detail=(id,cp=1)=>({id,account_id:1,items:[],components:[],items_pagination:{page:1,per_page:50,total:0,pages:0},components_pagination:{page:cp,per_page:50,total:51,pages:2}});
reply(6,detail(12));await b;reply(5,detail(11));await a;assert.equal(page.detail.id,12);assert.equal(page.$refs.drawer.open,true);assert.equal(document.body.style.overflow,'hidden');
page.detailPage('components',2);await new Promise(setImmediate);assert.equal(new URL(location).searchParams.get('component_page'),'2');reply(7,detail(12,2));await new Promise(setImmediate);assert.equal(page.detail.components_pagination.page,2);
page.dismissDetail();assert.equal(document.body.style.overflow,'');assert.equal(history.backCalled,true);
const expired=page.load();reply(8,{},401);await expired;assert.equal(page.sessionEnded,true);const count=requests.length;await page.load();await page.openDetail(3);assert.equal(requests.length,count);options.beforeUnmount.call(page);
const opts=ozonFinance.createOptions(config),p={...opts.data(),...opts.methods,$refs:{}};p.filters.page=1;const pending=p.load();opts.beforeUnmount.call(p);reply(9,body(55));await pending;assert.equal(p.items.length,0);
''')


def test_list_timeout_keeps_last_good_totals_and_allows_explicit_retry():
    run(r'''
const timers=new Map();let tid=0;global.setTimeout=(f,ms)=>{timers.set(++tid,{f,ms});return tid};global.clearTimeout=id=>timers.delete(id);
const options=ozonFinance.createOptions({accountId:1,api:'/api/finance'}),page={...options.data(),...options.methods};
page.items=[{id:1}];page.totals=[{currency:'RUB',net:'-10.0000'}];page.loaded=true;
global.fetch=(_url,opts)=>new Promise((_,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));
const pending=page.load();[...timers.values()].find(t=>t.ms===10000).f();await pending;
assert.equal(page.items[0].id,1);assert.equal(page.totals[0].net,'-10.0000');assert.equal(page.loading,false);assert.ok(page.error.includes('много времени'));assert.equal(timers.size,0);
''')


def test_pinned_snapshot_survives_initial_completed_status_but_active_refresh_opens_latest():
    run(r'''
location=new URL('https://fixture.test/marketplaces/finance?account_id=1&snapshot_id=9&as_of=2026-09-24&page=2');
const config={accountId:1,api:'/api/finance',page:'/marketplaces/finance'},options=ozonFinance.createOptions(config),page={...options.data(),...options.methods,$refs:{}};
const urls=[];let latest=10;
global.fetch=async url=>{urls.push(url);const q=new URL(url,location).searchParams,id=Number(q.get('snapshot_id')||latest),p=Number(q.get('page')||1);return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({success:true,data:{items:[{id}],totals:[],scope:{account_id:1},snapshot_sync:{id},coverage:{requested_end:q.get('as_of')||'2026-09-25'},pagination:{page:p,per_page:25,total:30,pages:2}}})}};
await page.onRefreshCompleted({fromActive:false});assert.equal(page.filters.snapshotId,'9');assert.equal(page.filters.page,2);assert.equal(page.filters.asOf,'2026-09-24');assert.ok(urls.at(-1).includes('snapshot_id=9'));assert.ok(urls.at(-1).includes('as_of=2026-09-24'));
page.applyFilters();await new Promise(setImmediate);assert.equal(page.filters.snapshotId,'9');
await page.onRefreshCompleted({fromActive:true});await new Promise(setImmediate);assert.equal(page.filters.snapshotId,'10');assert.equal(page.filters.asOf,'2026-09-25');assert.equal(page.filters.page,1);assert.ok(!urls.at(-1).includes('snapshot_id='));assert.equal(new URL(location).searchParams.get('snapshot_id'),'10');
let observed=[];const refresh=ozonReadRefresh({accountId:1,domain:'finance'});refresh.period='30d';refresh.onRefreshCompleted=async info=>observed.push(Boolean(info.fromActive));await refresh.acceptRefresh({account_id:1,domain:'finance',period:'30d',id:1,status:'completed',active:false},0,'30d',false);await refresh.acceptRefresh({account_id:1,domain:'finance',period:'30d',id:2,status:'waiting',active:true},0,'30d',true);await refresh.acceptRefresh({account_id:1,domain:'finance',period:'30d',id:2,status:'completed',active:false},0,'30d',false);assert.deepEqual(observed,[false,true]);
options.beforeUnmount.call(page);refresh.destroyRefresh();
''')


def test_completed_status_after_lost_post_opens_latest_without_second_post():
    run(r'''
location=new URL('https://fixture.test/marketplaces/finance?account_id=1&snapshot_id=9&as_of=2026-09-24');
const opts=ozonFinance.createOptions({accountId:1,api:'/api/finance',page:'/marketplaces/finance'}),page={...opts.data(),...opts.methods,$refs:{}};
let posts=0,listReads=0;
global.fetch=async (url,options={})=>{
 if(options.method==='POST'){posts++;throw new TypeError('Network failure after accept')}
 let data;
 if(url.includes('/sync'))data={account_id:1,domain:'finance',period:'30d',id:12,status:'completed',active:false};
 else{listReads++;assert.ok(!url.includes('snapshot_id='));data={items:[],totals:[],scope:{account_id:1},snapshot_sync:{id:10},coverage:{requested_end:'2026-09-25'},pagination:{page:1,per_page:25,total:0,pages:0}}}
 return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({data,success:true})}
};
await page.requestRefresh();assert.equal(page.refreshUncertain,true);await page.requestRefresh();assert.equal(posts,1);
await page.loadRefreshStatus();await new Promise(setImmediate);assert.equal(page.filters.snapshotId,'10');assert.equal(page.filters.asOf,'2026-09-25');assert.equal(posts,1);assert.equal(listReads,1);assert.equal(page.refreshUncertain,false);opts.beforeUnmount.call(page);
''')
