"""Vue controller behavior: stale reads, URL state, detail paging, session loss."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]


def test_fulfillment_controller_races_scope_url_and_detail():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for Vue controller checks')
    setup = r'''
const assert=require('node:assert/strict');global.window=global;
global.location=new URL('https://fixture.test/marketplaces/orders?account_id=1&search=abc&status=delivered&page=2');
global.history={state:{},pushState(state,_,url){this.state=state;location=new URL(url,location);},replaceState(state,_,url){this.pushState(state,_,url);},back(){this.backCalled=true;}};
const listeners=new Map();global.addEventListener=(k,v)=>listeners.set(k,v);global.removeEventListener=k=>listeners.delete(k);
global.document={hidden:false,getElementById:()=>null,body:{style:{overflow:''}},activeElement:{isConnected:true,focus(){this.focused=true;}},addEventListener:window.addEventListener,removeEventListener:window.removeEventListener};
global.mcatShared={imageDeadline:{}};
'''
    source = '\n'.join((ROOT / 'static' / name).read_text() for name in ['ozon-read-refresh.js','ozon-fulfillment.js'])
    scenario = r'''
(async()=>{
 const config={accountId:1,kind:'orders',csrfToken:'test',api:{orders:'/api/orders'},pages:{orders:'/marketplaces/orders'}};
 const options=ozonFulfillment.createOptions(config);
 const page={...options.data(),...options.methods,$nextTick:async()=>{},$refs:{drawer:{open:false,showModal(){this.open=true;},close(){this.open=false;}}}};
 for(const [k,f] of Object.entries(options.computed))Object.defineProperty(page,k,{get:()=>f.call(page)});
 assert.equal(page.filters.page,2);assert.equal(page.filters.search,'abc');assert.equal(page.filters.status,'delivered');
 assert.ok(ozonFulfillment.money(0,'RUB').startsWith('0'));assert.ok(ozonFulfillment.money('0',null).includes('валюта не указана'));
 for(const v of [null,undefined,'',true,'NaN','1xx','-1'])assert.equal(ozonFulfillment.money(v,'RUB'),'Цена не указана');
 assert.equal(page.safeImage('javascript:alert(1)'),'');assert.equal(page.safeImage('https://user:password@host/p.jpg'),'');
 assert.equal(page.statusLabel('new_provider_status'),'new_provider_status');
 let requests=[];global.fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
 const body=(id,p=2,aid=1)=>({items:[{id}],pagination:{page:p,per_page:25,total:30,pages:2},scope:{account_id:aid},status_counts:{delivered:30}});
 const reply=(n,data,status=200)=>requests[n].resolve({ok:status===200,status,headers:{get:()=> 'application/json'},json:async()=>({success:status===200,data})});
 const old=page.load();page.filters={...page.filters,period:'7d',page:1};page.period='7d';const fresh=page.load();
 assert.equal(requests[0].options.signal.aborted,true);
 reply(1,body(7,1));await fresh;reply(0,body(30));await old;
 assert.equal(page.items[0].id,7);assert.equal(page.observedFilters.period,'7d');
 const wrong=page.load();reply(2,body(90,1,2));await wrong;assert.equal(page.items[0].id,7);assert.ok(page.error.includes('магазина'));
 const malformed=page.load();reply(3,{...body(90,1),pagination:{page:1,per_page:25,total:30,pages:999}});await malformed;assert.equal(page.items[0].id,7);
 page.searchDraft=' Новый ';page.applyFilters();assert.equal(location.search.includes('page='),false);assert.equal(new URL(location).searchParams.get('search'),'Новый');
 reply(4,body(8,1));await new Promise(setImmediate);assert.equal(page.items[0].id,8);
 const a=page.openDetail(11);await new Promise(setImmediate);const b=page.openDetail(12);await new Promise(setImmediate);
 const detail=id=>({id,account_id:1,posting_number:'order-'+id,items:[],item_pagination:{page:1,per_page:50,total:0,pages:0}});
 reply(6,detail(12));await b;reply(5,detail(11));await a;
 assert.equal(page.detail.id,12);assert.equal(page.$refs.drawer.open,true);assert.equal(document.body.style.overflow,'hidden');
 assert.equal(new URL(location).searchParams.get('posting_id'),'12');
 page.dismissDetail();assert.equal(page.$refs.drawer.open,false);assert.equal(document.body.style.overflow,'');assert.equal(history.backCalled,true);
 const expired=page.load();reply(7,{},401);await expired;assert.equal(page.sessionEnded,true);const count=requests.length;await page.load();await page.openDetail(2);assert.equal(requests.length,count);
 options.beforeUnmount.call(page);
 // The unmount revision gate also handles transports that ignore abort.
 const options2=ozonFulfillment.createOptions(config),p2={...options2.data(),...options2.methods,$refs:{}};
 const pending=p2.load();options2.beforeUnmount.call(p2);reply(8,body(99,1));await pending;assert.equal(p2.items.length,0);
})().then(()=>console.log('SCENARIO_COMPLETED')).catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node,'-e',setup+source+scenario],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'SCENARIO_COMPLETED' in result.stdout, result.stderr


def test_refresh_timeout_unknown_post_and_access_end_stop_writes():
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=(ROOT/'static/ozon-read-refresh.js').read_text()
    script=r'''
const assert=require('node:assert/strict');global.window=global;const timers=new Map();let tid=0;
global.setTimeout=(f,ms)=>{timers.set(++tid,{f,ms});return tid;};global.clearTimeout=id=>timers.delete(id);
global.document={hidden:false,addEventListener:()=>{},removeEventListener:()=>{}};
'''+source+r'''
(async()=>{
 let requests=0;
 global.fetch=(url,options)=>{requests++;return new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));};
 const p={...ozonReadRefresh({accountId:1,domain:'fulfillment',csrfToken:'x'}),period:'30d',onRefreshCompleted:async()=>{}};
 const start=p.requestRefresh();assert.equal(requests,1);
 [...timers.values()].find(t=>t.ms===10000).f();await start;
 assert.equal(p.refreshUncertain,true);assert.equal(p.refreshing,true);assert.ok(p.refreshStatusError.includes('могла начаться'));
 await p.requestRefresh();assert.equal(requests,1);
 global.fetch=async()=>{requests++;return {ok:false,status:403,json:async()=>({error:'forbidden'})};};
 await p.loadRefreshStatus();assert.equal(p.refreshSessionEnded,true);assert.equal(timers.size,0);
 await p.requestRefresh();await p.loadRefreshStatus();assert.equal(requests,2);
 p.destroyRefresh();
})().then(()=>console.log('SCENARIO_COMPLETED')).catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert 'SCENARIO_COMPLETED' in result.stdout, result.stderr
