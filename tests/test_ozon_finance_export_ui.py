"""File download scope, cancellation, session end, size/error recovery."""
from tests.test_ozon_finance_ui import run

SETUP=r'''
location=new URL('https://fixture.test/marketplaces/finance?account_id=1&snapshot_id=9&as_of=2026-09-25&search=_100%25&sign=negative&page=2');
const config={accountId:1,api:'/api/finance',exportApi:'/api/finance/export.xlsx',page:'/marketplaces/finance'};
const options=ozonFinance.createOptions(config),page={...options.data(),...options.methods,$refs:{}};
for(const [k,f] of Object.entries(options.computed))Object.defineProperty(page,k,{get:()=>f.call(page)});
page.loaded=true;page.observedFilters={...page.filters};
const clicks=[],objects=[];global.URL.createObjectURL=b=>{objects.push(b);return 'blob:synthetic'};global.URL.revokeObjectURL=()=>{};
document.body.appendChild=()=>{};document.createElement=()=>({click(){clicks.push({href:this.href,download:this.download})},remove(){}});
const mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const reply=(patch={})=>({ok:true,status:200,headers:{get:k=>({'content-type':mime,'x-finance-account-id':'1','x-finance-snapshot-id':'9','x-finance-as-of':'2026-09-25','x-finance-period':'30d','content-disposition':'attachment; filename="ozon-finance-1.xlsx"'}[k]||null)},blob:async()=>new Blob(['synthetic xlsx']),...patch});
'''


def test_download_has_exact_filters_all_pages_and_can_be_cancelled():
    run(SETUP+r'''
let requests=[];global.fetch=(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}));
const a=page.exportExcel();await page.exportExcel();assert.equal(requests.length,1);assert.equal(page.exporting,true);
const q=new URL(requests[0].url,location).searchParams;assert.equal(q.get('search'),'_100%');assert.equal(q.get('snapshot_id'),'9');assert.equal(q.get('as_of'),'2026-09-25');assert.equal(q.get('sign'),'negative');assert.equal(q.has('page'),false);
requests[0].resolve(reply());await a;assert.equal(clicks.length,1);assert.equal(clicks[0].download,'ozon-finance-1.xlsx');assert.ok(page.exportMessage.includes('вся выбранная выборка'));assert.equal(page.exporting,false);
const cancelled=page.exportExcel();page.cancelExport();requests[1].resolve(reply());await cancelled;assert.equal(requests[1].opts.signal.aborted,true);assert.equal(clicks.length,1);assert.equal(page.exporting,false);
const changed=page.exportExcel();page.filters.sign='positive';requests[2].resolve(reply());await changed;assert.equal(clicks.length,1);
options.beforeUnmount.call(page);
''')


def test_export_failures_never_download_json_foreign_or_oversized_file():
    run(SETUP+r'''
const original=reply();let response;
global.fetch=async()=>response;
response=reply({ok:false,status:422,json:async()=>({error:'Сократите период'})});await page.exportExcel();assert.equal(clicks.length,0);assert.equal(page.exportError,'Сократите период');
response=reply({headers:{get:k=>k==='x-finance-account-id'?'2':original.headers.get(k)}});await page.exportExcel();assert.equal(clicks.length,0);assert.ok(page.exportError.includes('магазин'));
response=reply({headers:{get:k=>k==='content-type'?'text/html':original.headers.get(k)}});await page.exportExcel();assert.equal(clicks.length,0);
response=reply({blob:async()=>({size:16*1024*1024+1})});await page.exportExcel();assert.equal(clicks.length,0);
response=reply({status:401,ok:false});await page.exportExcel();assert.equal(page.sessionEnded,true);assert.equal(page.refreshSessionEnded,true);assert.equal(page.canExport,false);assert.equal(clicks.length,0);options.beforeUnmount.call(page);
''')


def test_export_timeout_and_unmount_abort_without_late_download():
    run(SETUP+r'''
const timers=new Map();let n=0;global.setTimeout=(f,ms)=>{timers.set(++n,{f,ms});return n};global.clearTimeout=k=>timers.delete(k);
global.fetch=(_url,opts)=>new Promise((_,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));
const timeout=page.exportExcel();[...timers.values()].find(t=>t.ms===30000).f();await timeout;assert.equal(page.exporting,false);assert.ok(page.exportError.includes('много времени'));assert.equal(clicks.length,0);
const pending=page.exportExcel();options.beforeUnmount.call(page);await pending;assert.equal(clicks.length,0);assert.equal(timers.size,0);
''')
