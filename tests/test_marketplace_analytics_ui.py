"""Exact observed metrics, pinned navigation and asynchronous Vue lifecycle."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).parents[1]


def run(body):
    node=shutil.which('node')
    if not node:pytest.skip('Node is needed for frontend behavior checks')
    prelude=r'''
const assert=require('node:assert/strict');global.window=global;
let events={};global.document={hidden:false,visibilityState:'visible',getElementById:()=>null,addEventListener:()=>{},removeEventListener:()=>{}};
global.addEventListener=(name,callback)=>events[name]=callback;global.removeEventListener=name=>delete events[name];
global.mcatShared={imageDeadline:{}};global.location=new URL('https://fixture.test/marketplaces/analytics?account_id=1');
global.history={state:{},replaceState:(s,_,url)=>{location=new URL(url,location)},pushState:(s,_,url)=>{location=new URL(url,location)}};
'''
    source=(ROOT/'static/ozon-read-refresh.js').read_text()+'\n'+(ROOT/'static/ozon-analytics.js').read_text()
    setup=r'''
const config={accountId:1,api:'/api/workspace',page:'/marketplaces/analytics',csrfToken:'synthetic'};
function makePage(){const options=ozonAnalytics.createOptions(config),page={...options.data(),...options.methods};for(const [k,f] of Object.entries(options.computed))Object.defineProperty(page,k,{get:()=>f.call(page)});page.changeRefreshPeriod=()=>{};return {page,options};}
function payload(patch={}){return {scope:{account_id:1,marketplace_code:'ozon',cross_marketplace_comparable:false},filters:{period:'30d',search:'',sort_by:'ordered_revenue_rub',sort_dir:'desc'},as_of:'2026-09-25',status:'ready',snapshot:{id:9,period_start:'2026-08-27',period_end:'2026-09-25',completed_at:'2026-09-25T10:00:00',period_matches_request:true},totals:{ordered_revenue_rub:'1200.0000',ordered_units:'4.0000',average_unit_rub:'300.00'},daily:[{date:'2026-09-24',ordered_revenue_rub:null,ordered_units:null},{date:'2026-09-25',ordered_revenue_rub:'1200.0000',ordered_units:'4.0000'}],products:[{sku:'123',title:'Наблюдённый товар',metrics:{ordered_revenue_rub:'1200.0000',ordered_units:'4.0000'}}],pagination:{page:1,per_page:25,total:1,pages:1},requested_period:{start:'2026-08-27',end:'2026-09-25'},...patch};}
function reply(data,patch={}){return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({success:true,data}),...patch};}
'''
    script=prelude+'\n'+source+'\n'+setup+'\n(async()=>{'+body+'\n})().catch(e=>{console.error(e);process.exitCode=1});'
    subprocess.run([node,'-e',script],check=True,capture_output=True,text=True,timeout=10)


def test_exact_decimal_labels_unknown_zero_and_coherent_shares():
    run(r'''
assert.equal(ozonAnalytics.decimal('9007199254740993.0012',true),'9\u00a0007\u00a0199\u00a0254\u00a0740\u00a0993,0012\u00a0₽');
assert.equal(ozonAnalytics.decimal('0.0000',true),'0,00\u00a0₽');assert.equal(ozonAnalytics.decimal('0.0000'),'0');
assert.equal(ozonAnalytics.decimal('2.2500'),'2,25');assert.equal(ozonAnalytics.decimal(null),'—');assert.equal(ozonAnalytics.decimal('NaN'),'—');
assert.equal(ozonAnalytics.percent('1.0000','3.0000'),33.33);assert.equal(ozonAnalytics.percent('0','3'),0);assert.equal(ozonAnalytics.percent('1','0'),null);assert.equal(ozonAnalytics.percent('4','3'),null);
const {page,options}=makePage();page.daily=payload().daily;assert.equal(page.chart.known,1);assert.equal(page.chart.bars[0].known,false);assert.equal(page.chart.bars[1].height,150);options.beforeUnmount.call(page);
''')


def test_late_and_aborted_response_cannot_replace_current_selection():
    run(r'''
const {page,options}=makePage(),requests=[];global.fetch=(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}));
const older=page.load();page.filters.search='новое';const current=page.load();assert.equal(requests[0].opts.signal.aborted,true);
requests[1].resolve(reply(payload({filters:{...payload().filters,search:'новое'}})));await current;
requests[0].resolve(reply(payload({products:[{sku:'old'}]})));await older;assert.equal(page.products[0].sku,'123');assert.equal(page.observedFilters.search,'новое');assert.equal(page.error,'');
assert.equal(page.filters.snapshotId,'9');assert.equal(page.filters.asOf,'2026-09-25');assert.equal(new URL(location).searchParams.get('snapshot_id'),'9');options.beforeUnmount.call(page);
''')


def test_pinned_snapshot_dates_and_account_are_checked_before_accepting_data():
    run(r'''
const {page,options}=makePage();global.fetch=async()=>reply(payload());await page.load();const original=page.products;
for(const bad of [payload({snapshot:{...payload().snapshot,id:10}}),payload({as_of:'2026-09-26'}),payload({scope:{account_id:2,marketplace_code:'ozon',cross_marketplace_comparable:false}})]){
 global.fetch=async()=>reply(bad);await page.load();assert.equal(page.products,original);assert.ok(page.error);assert.equal(page.filters.snapshotId,'9');
}
options.beforeUnmount.call(page);
''')


def test_refresh_initial_completion_preserves_pin_but_observed_completion_opens_latest():
    run(r'''
const {page,options}=makePage();let calls=0;global.fetch=async()=>{calls++;return reply(payload())};await page.load();
await page.onRefreshCompleted({fromActive:false,afterUncertain:false});assert.equal(calls,1);assert.equal(page.filters.snapshotId,'9');
let patch;page.navigate=value=>patch=value;await page.onRefreshCompleted({fromActive:true});assert.equal(patch.snapshotId,'');assert.equal(patch.asOf,'');
patch=null;await page.onRefreshCompleted({afterUncertain:true});assert.equal(patch.snapshotId,'');options.beforeUnmount.call(page);
''')


def test_session_end_stops_reads_and_timeout_keeps_previous_data():
    run(r'''
const {page,options}=makePage();let calls=0;global.fetch=async()=>{calls++;return reply(payload())};await page.load();const original=page.products;
const timers=new Map();let n=0;global.setTimeout=(f,ms)=>{timers.set(++n,{f,ms});return n};global.clearTimeout=k=>timers.delete(k);
global.fetch=(_,opts)=>new Promise((_,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));
const slow=page.load();[...timers.values()].find(x=>x.ms===10000).f();await slow;assert.equal(page.products,original);assert.ok(page.error.includes('много времени'));assert.equal(page.loading,false);
global.fetch=async()=>{calls++;return reply(null,{ok:false,status:401})};await page.load();const ended=calls;assert.equal(page.sessionEnded,true);assert.equal(page.refreshSessionEnded,true);await page.load();assert.equal(calls,ended);options.beforeUnmount.call(page);
''')


def test_unmount_drops_late_data_and_daily_controls_do_not_fetch():
    run(r'''
const {page,options}=makePage();let calls=0,resolve,signal;global.fetch=(_,opts)=>{calls++;signal=opts.signal;return new Promise(r=>resolve=r)};
page.daily=payload().daily;page.changeMetric('ordered_units');page.selectedDay='2026-09-24';page.toggleDays();assert.equal(calls,0);assert.equal(new URL(location).searchParams.get('view'),'days');assert.equal(new URL(location).searchParams.get('metric'),'ordered_units');
const pending=page.load();options.beforeUnmount.call(page);assert.equal(signal.aborted,true);resolve(reply(payload()));await pending;assert.equal(page.loaded,false);assert.deepEqual(page.products,[]);
''')


def test_navigation_preserves_filters_pin_and_browser_back_state():
    run(r'''
const {page,options}=makePage();page.initRefresh=()=>{};let reads=[];page.load=async()=>reads.push({...page.filters});options.mounted.call(page);
page.filters.snapshotId='9';page.filters.asOf='2026-09-25';page.navigate({search:'_100%',sortBy:'ordered_units',sortDir:'asc',page:2});
const q=new URL(location).searchParams;assert.equal(q.get('search'),'_100%');assert.equal(q.get('sort_by'),'ordered_units');assert.equal(q.get('snapshot_id'),'9');assert.equal(q.get('page'),'2');
location=new URL('https://fixture.test/marketplaces/analytics?account_id=1&period=7d&snapshot_id=8&as_of=2026-09-24&page=3&metric=ordered_units&view=days');events.popstate();
assert.equal(page.period,'7d');assert.equal(page.filters.snapshotId,'8');assert.equal(page.filters.page,3);assert.equal(page.showDailyTable,true);assert.equal(reads.at(-1).period,'7d');options.beforeUnmount.call(page);
''')
