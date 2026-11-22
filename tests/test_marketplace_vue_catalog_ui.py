"""Primary Vue catalogue: account scope, factual money and asynchronous detail reads."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]


def run_node(body):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for JavaScript behavior checks')
    completed = subprocess.run([node, '-e', body], cwd=ROOT, text=True, capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_display_preserves_cents_zero_and_unknown_facts():
    run_node(r'''
const assert=require('node:assert/strict');global.window=global;
require('./static/marketplace-beta-shared.js');const S=window.mcatShared;
const row=(value,currency='RUB')=>({price_summary:{values:{price:value},currency}});
assert.equal(S.priceLabel(row('1299.50')),'1\u00a0299,5 ₽');
assert.equal(S.priceLabel(row('0')),'0 ₽');
assert.equal(S.priceLabel({price_summary:{available:false,values:{price:'1200'}}}),'—');
for(const invalid of ['12garbage','',true,-10,'NaN','Infinity',{},null]) assert.equal(S.priceLabel(row(invalid)),'—');
assert.equal(S.priceLabel(row('12',null)),'12');
assert.equal(S.priceLabel({price_summary:{price:1000,discount_price:0,source:'legacy_wb_projection'}}),'0 ₽');
assert.equal(S.stockLabel({stock_summary:{present:0}}),'0 шт');
for(const invalid of [-1,1.5,'12',true,NaN,Infinity]) assert.equal(S.stockLabel({stock_summary:{present:invalid}}),'—');
assert.equal(S.stockLabel({stock_summary:{present:0,available:false}}),'—');
assert.equal(S.stockRows({stock_summary:{by_type:{fbo:{}}}})[0].value,'—');
''')


def test_ozon_base_seller_promotion_and_buyer_are_separate_and_no_false_discount():
    run_node(r'''
const assert=require('node:assert/strict');global.window=global;
require('./static/marketplace-beta-shared.js');const S=window.mcatShared;
const listing={marketplace_code:'ozon',prices_synced_at:'2026-09-24T20:36:45',price_summary:{currency:'RUB',values:{old_price:'1462',price:'1059',marketing_seller_price:0,marketing_price:799,retail_price:700}}};
assert.equal(S.priceLabel(listing),'1\u00a0059 ₽');
let facts=S.priceFacts(listing);
assert.equal(facts.base,1462);assert.equal(facts.seller,1059);assert.equal(facts.sellerPromotion,null);
assert.equal(facts.buyer,null);assert.equal(facts.marketplaceDiscount,null);assert.equal(S.discountPercent(listing),null);
assert.equal(facts.observedAt,listing.prices_synced_at);
listing.price_summary.values.marketing_seller_price='900';facts=S.priceFacts(listing);
assert.equal(facts.sellerPromotion,900);assert.equal(S.priceLabel(listing),'1\u00a0059 ₽');
for(const bad of [null,0,'0.00',true,-1,'NaN','12garbage']) {
 listing.price_summary.values.old_price=bad;listing.price_summary.values.marketing_seller_price=bad;
 facts=S.priceFacts(listing);assert.equal(facts.base,null);assert.equal(facts.sellerPromotion,null);
 assert.equal(facts.seller,1059);assert.equal(facts.marketplaceDiscount,null);
}
delete listing.price_summary.values.price;assert.equal(S.priceLabel(listing),'—');
listing.price_summary.values={old_price:1000,price:1059,marketing_seller_price:900};
facts=S.priceFacts(listing);assert.equal(facts.base,1000);assert.equal(S.discountPercent(listing),null);
listing.price_summary.available=false;facts=S.priceFacts(listing);
assert.equal(facts.base,null);assert.equal(facts.seller,null);assert.equal(facts.sellerPromotion,null);
const wb={price_summary:{price:1500,discount_price:1200,source:'legacy_wb_projection'}};
assert.equal(S.priceLabel(wb),'1\u00a0200 ₽');assert.equal(S.priceFacts(wb).buyer,null);
''')


SETUP = r'''
const assert=require('node:assert/strict');global.window=global;let definition;
const account=id=>({id,label:'Магазин '+id,is_active:true,has_credentials:true,connection_status:'connected'});
const bootstrap={ozonEnabled:true,accounts:[account(1),account(2)],filters:{marketplace_code:'ozon',account_id:2},urls:{base:'/marketplaces/listings/',drafts:'/marketplaces/drafts',accountsPage:'/marketplaces/accounts'}};
global.document={getElementById:id=>id==='mcat-bootstrap'?{textContent:JSON.stringify(bootstrap)}:id==='marketplace-catalog-app'?{}:null,querySelector:()=>null};
global.localStorage={getItem:()=>{throw Error('blocked')},setItem:()=>{throw Error('blocked')}};
global.Vue={createApp:options=>{definition=options;return {mount(){}}}};
require('./static/marketplace-beta-shared.js');require('./static/marketplace-catalog-beta.js');
const page=definition.data();Object.entries(definition.methods).forEach(([k,v])=>page[k]=v.bind(page));
Object.entries(definition.computed).forEach(([k,v])=>Object.defineProperty(page,k,{get:v.bind(page)}));
page.$nextTick=fn=>fn();page.focusSelected=()=>{};
'''


def test_empty_account_is_not_misreported_as_empty_search_and_storage_is_optional():
    run_node(SETUP + r'''
assert.equal(page.view,'grid');page.setView('table');assert.equal(page.view,'table');
assert.deepEqual(page.visibleAccounts.map(a=>a.id),[2]);
assert.equal(page.syncableAccount.id,2);assert.equal(page.emptyState.action,'sync');
assert.equal(page.draftsUrl,'/marketplaces/drafts?account_id=2');
page.filters.search='нет';assert.equal(page.emptyState.action,'reset');page.resetFilters();
assert.equal(page.filters.account_id,2);assert.equal(page.filters.marketplace,'ozon');
page.accounts[1].credential_expires_at='2020-01-01T00:00:00';
assert.equal(page.canSync(page.accounts[1]),false);assert.equal(page.accountState(page.accounts[1]).label,'Ключ истёк');
assert.equal(page.syncableAccount,null);assert.equal(page.emptyState.action,'accounts');
page.filters.marketplace='wb';page.filters.account_id=null;
assert.deepEqual(page.visibleAccounts,[]);assert.match(page.emptyState.title,/Wildberries/);
''')


def test_full_catalog_completion_is_separate_from_current_sync_and_updates_from_status_map():
    run_node(SETUP + r'''
(async()=>{
const acc=page.accounts[1];
acc.last_completed_at='2026-11-19T08:10:00Z';
acc.sync_job={status:'running',next_retry_at:'2026-11-20T10:15:00Z',message:'Загрузка продолжится позже'};
acc.last_sync={status:'failed',error_message:'429'};
assert.equal(page.lastCompletedAt(acc),'2026-11-19T08:10:00Z');
assert.match(page.formattedLastCompletedAt(acc),/^19\.11\.2026.*UTC$/);
assert.ok(page.syncDate(acc).startsWith('Продолжим после '));
assert.match(page.syncHint(acc),/Загрузка продолжится позже/);
page.urls.accountsStatus='/accounts/status';
global.document.hidden=false;
global.fetch=async url=>{
    assert.equal(url,'/accounts/status');
    return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({success:true,
        onboarding_jobs:{'2':{status:'failed',message:'Попытка завершилась ошибкой'}},
        catalog_syncs:{'2':{status:'failed',error_message:'429'}},
        catalog_completed_at:{'1':null,'2':'2026-11-20T09:40:00Z'},
        accounts:[{id:2,is_active:false,has_credentials:false,connection_status:'unchecked'}],ozon_enabled:true})};
};
await page.pollSync();
assert.equal(acc.last_completed_at,'2026-11-20T09:40:00Z');
assert.equal(page.lastCompletedAt(acc),'2026-11-20T09:40:00Z');
assert.equal(page.syncFailed(acc),true);
assert.equal(page.canSync(acc),false);
assert.equal(acc.sync_job.status,'failed');
assert.equal(acc.last_sync.status,'failed');
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_manual_catalog_cooldown_is_exact_visible_and_blocks_both_sync_actions():
    run_node(SETUP + r'''
(async()=>{
const timers=new Map();let nextTimer=0;global.setTimeout=(fn,ms)=>{timers.set(++nextTimer,{fn,ms});return nextTimer};global.clearTimeout=id=>timers.delete(id);
const acc=page.accounts[1];const future=new Date(Date.now()+60*60*1000).toISOString();
acc.sync_job={status:'failed',manual_retry_at:future,next_retry_at:'2099-01-01T00:00:00Z',message:'Пауза Ozon'};
acc.last_sync={status:'failed'};page.cooldownClock=Date.now();
assert.equal(page.syncCooldownAt(acc),future);assert.equal(page.syncCoolingDown(acc),true);
assert.match(page.formattedSyncCooldownAt(acc),/UTC$/);assert.equal(page.syncDate(acc),'');
let posts=0,confirms=0;global.fetch=async()=>{posts++;throw Error('sync POST must be blocked')};global.confirm=()=>{confirms++;return true};
page.syncAccount(acc,false);page.syncAccount(acc,true);await new Promise(setImmediate);
assert.equal(posts,0);assert.equal(confirms,0);assert.equal(acc._syncing,false);

acc.sync_job={status:'running',manual_retry_at:future,next_retry_at:future,message:'Продолжим позже'};
assert.equal(page.syncCoolingDown(acc),false);assert.ok(page.syncDate(acc).startsWith('Продолжим после '));
page.syncAccount(acc,false);assert.equal(posts,0,'active work continues to suppress manual POSTs');

for(const deadline of ['invalid-deadline',new Date(Date.now()-5000).toISOString()]){
    acc.sync_job={status:'failed',manual_retry_at:deadline,message:'Попытка завершилась'};
    assert.equal(page.syncCoolingDown(acc),false);assert.equal(page.syncDate(acc),'');
    global.fetch=async()=>{posts++;return {ok:true,status:202,headers:{get:()=> 'application/json'},json:async()=>({success:true,job:{account_id:2,status:'completed',message:'Загрузка запущена'}})}};
    page.syncAccount(acc,false);await new Promise(setImmediate);await new Promise(setImmediate);
    assert.equal(acc.sync_job.status,'completed');
}
assert.equal(posts,2,'past and invalid public deadlines keep the ordinary request path');
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_cooldown_conflict_rechecks_local_status_once_without_replaying_post():
    run_node(SETUP + r'''
(async()=>{
const timers=new Map();let nextTimer=0;global.setTimeout=(fn,ms)=>{timers.set(++nextTimer,{fn,ms});return nextTimer};global.clearTimeout=id=>timers.delete(id);
const acc=page.accounts[1];let posts=0,gets=0;page.urls.accountsStatus='/accounts/status';
const retryAt=new Date(Date.now()+60*60*1000).toISOString();let unauthorized=false;
global.fetch=async(url,options={})=>{
    if(options.method==='POST'){
        posts++;const body={success:false,code:'ozon_catalog_cooldown',error:'Озон запросил паузу'};
        return {ok:false,status:409,headers:{get:()=> 'application/json'},clone:()=>({json:async()=>body}),json:async()=>body};
    }
    gets++;assert.equal(url,'/accounts/status');
    if(unauthorized)return {ok:false,status:401,headers:{get:()=> 'application/json'},json:async()=>({success:false,error:'Session expired'})};
    return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({success:true,
        onboarding_jobs:{'2':{status:'failed',manual_retry_at:retryAt,message:'Озон запросил паузу'}},
        catalog_syncs:{'2':{status:'failed'}},catalog_completed_at:{'2':null},
        accounts:[{id:2,is_active:true,has_credentials:true,connection_status:'connected'}],ozon_enabled:true})};
};
page.syncAccount(acc,false);await new Promise(setImmediate);await new Promise(setImmediate);await new Promise(setImmediate);
assert.equal(posts,1);assert.equal(gets,1);assert.equal(page.syncCoolingDown(acc),true);
assert.equal(acc._syncing,false);assert.equal(acc._error,null);assert.equal(page.syncDate(acc),'');
acc.sync_job={status:'failed',manual_retry_at:null};acc._error=null;page.syncSessionExpired=false;page.syncError='';unauthorized=true;
page.syncAccount(acc,false);await new Promise(setImmediate);await new Promise(setImmediate);await new Promise(setImmediate);
assert.equal(posts,2);assert.equal(gets,2);assert.equal(page.syncCoolingDown(acc),false);
assert.equal(page.syncSessionExpired,true);assert.match(page.syncError,/Сессия истекла/);
assert.match(acc._error,/Озон запросил паузу/);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_late_detail_failure_does_not_replace_new_card_and_keyboard_keeps_link_behavior():
    run_node(SETUP + r'''
(async()=>{
const pending=[];global.fetch=(url,opts)=>new Promise((resolve,reject)=>pending.push({url,opts,resolve,reject}));
page.items=[{listings:[{id:1}]},{listings:[{id:2}]}];page.selectedIndex=0;page.drawer.open=true;
page.loadDetail();page.selectedIndex=1;page.loadDetail();
assert.equal(pending[0].opts.signal.aborted,true);
pending[1].resolve({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({listing:{id:2,title:'Current'},success:true})});
await new Promise(setImmediate);await new Promise(setImmediate);
pending[0].reject(Error('Old failure'));await new Promise(setImmediate);
assert.equal(page.detail.title,'Current');assert.equal(page.drawer.error,null);
page.closeDrawer();assert.equal(pending[1].opts.signal.aborted,true);
let prevented=false;page.onKey({key:'Enter',target:{tagName:'A',closest:()=>({})},preventDefault:()=>prevented=true});
assert.equal(prevented,false);assert.equal(page.drawer.open,false);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_vue_connection_owns_one_lifecycle_without_provider_calls_or_secret_state():
    run_node(r'''
const assert=require('node:assert/strict');global.window=global;
global.location={href:'https://seller.test/marketplaces/accounts/'};
const timers=new Map(),listeners=new Map();let next=0,definition;
global.setTimeout=(fn,ms)=>{timers.set(++next,{fn,ms});return next};global.clearTimeout=id=>timers.delete(id);
const bootstrap={accounts:[{id:7,is_active:true,has_credentials:true,connection_status:'connected'}],onboarding_jobs:{7:{status:'pending',processed:0}},status_url:'/status'};
const root={dataset:{ozonSetup:JSON.stringify(bootstrap)}};
global.document={hidden:false,getElementById:id=>id==='ozon-account-setup'?root:null,addEventListener:(key,fn)=>listeners.set(key,fn),removeEventListener:key=>listeners.delete(key)};
global.Vue={createApp:opts=>{definition=opts;return {mount(el){assert.equal(el,root)}}}};
global.fetch=()=>{throw Error('Mount must not call a provider or enqueue work')};
require('./static/ozon-account-setup.js');require('./static/ozon-account-setup-vue.js');
const page=definition.data();Object.entries(definition.methods).forEach(([key,fn])=>page[key]=fn.bind(page));
definition.mounted.call(page);assert.equal(timers.size,1);assert.equal(listeners.size,1);
assert.equal(page.active(7),true);assert.equal(page.connected(7),true);
assert.equal(Object.hasOwn(page,'api_key'),false);
document.hidden=true;listeners.get('visibilitychange')();assert.equal(timers.size,0);
definition.beforeUnmount.call(page);assert.equal(timers.size,0);assert.equal(listeners.size,0);
''')


IMAGE_SETUP = r'''
const assert=require('node:assert/strict');global.window=global;
const timers=new Map();let next=0;const observations=[];
global.setTimeout=fn=>{timers.set(++next,fn);return next};global.clearTimeout=id=>timers.delete(id);
global.IntersectionObserver=class {constructor(fn){this.fn=fn;this.disconnected=false;observations.push(this)}observe(){}disconnect(){this.disconnected=true}};
require('./static/marketplace-beta-shared.js');const directive=window.mcatShared.imageDeadline;
class Img extends EventTarget {constructor(src,lazy=false){super();this.src=src;this.loading=lazy?'lazy':'eager';this.complete=false;this.naturalWidth=0;this.failures=0;this.addEventListener('error',()=>this.failures++)}getAttribute(){return this.src}removeAttribute(){this.src=null}}
'''


def test_hanging_image_aborts_once_and_does_not_retry_itself():
    run_node(IMAGE_SETUP + r'''
const img=new Img('https://img.example/hanging.jpg');directive.mounted(img);
assert.equal(timers.size,1);const expire=[...timers.values()][0];expire();
assert.equal(img.src,null);assert.equal(img.failures,1);assert.equal(timers.size,0);
expire();assert.equal(img.failures,1);
''')


def test_lazy_image_gets_no_deadline_before_visibility_and_load_cancels_it():
    run_node(IMAGE_SETUP + r'''
const img=new Img('https://img.example/lazy.jpg',true);directive.mounted(img);
assert.equal(timers.size,0);observations[0].fn([{isIntersecting:false}]);assert.equal(timers.size,0);
observations[0].fn([{isIntersecting:true}]);assert.equal(timers.size,1);assert.equal(observations[0].disconnected,true);
img.complete=true;img.naturalWidth=800;img.dispatchEvent(new Event('load'));
assert.equal(timers.size,0);directive.updated(img);assert.equal(timers.size,0);assert.equal(img.failures,0);
''')


def test_old_image_timeout_cannot_remove_new_image_and_unmount_cleans_up():
    run_node(IMAGE_SETUP + r'''
const img=new Img('https://img.example/first.jpg');directive.mounted(img);
const old=[...timers.values()][0];img.src='https://img.example/second.jpg';directive.updated(img);
old();assert.equal(img.src,'https://img.example/second.jpg');assert.equal(img.failures,0);assert.equal(timers.size,1);
directive.updated(img);assert.equal(timers.size,1);
directive.beforeUnmount(img);assert.equal(timers.size,0);
const lazy=new Img('https://img.example/offscreen.jpg',true);directive.mounted(lazy);directive.beforeUnmount(lazy);
observations[0].fn([{isIntersecting:true}]);assert.equal(timers.size,0);assert.equal(observations[0].disconnected,true);
''')


def test_ozon_listing_update_selection_is_one_shop_exact_and_survives_loaded_pages_and_view_toggle():
    run_node(SETUP + r'''
(async()=>{
const listing=(id,accountId=2)=>({id,account_id:accountId,marketplace_code:'ozon',title:'Ozon '+id,offer_id:'offer-'+id});
const group=(row)=>({key:'l'+row.id,listing_count:1,listings:[row],_p:row});
const stored=new Map();global.sessionStorage={getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)};
page.listingUpdateOwner='user-5:seller-8';
page.mode='listings';page.filters={marketplace:'ozon',account_id:2,status:'active',link_status:'',include_unavailable:false,search:'shoe'};
page.items=[group(listing(101)),group(listing(201,1)),{key:'mixed',listing_count:2,listings:[listing(301),listing(302)],_p:listing(301)}];
assert.equal(page.listingUpdateScopeEligible,true);
assert.deepEqual(page.visibleListingUpdateIds,[101]);
assert.equal(page.listingUpdateCandidateId(page.items[1]),null,'a different shop cannot enter the set');
assert.equal(page.listingUpdateCandidateId(page.items[2]),null,'a grouped/multi-listing row cannot enter the set');
assert.equal(page.listingUpdateCanSwitchToListings,false);
page.toggleListingUpdate(page.items[0],true);assert.deepEqual(page.listingUpdate.selectedIds,[101]);
page.urls.api='/api/listings';page.pagination={page:1,pages:2,total:2,has_next:true};page.perPage=1;
global.fetch=async(url)=>{
    assert.match(url,/page=2/);
    return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({success:true,
        items:[listing(102)],pagination:{page:2,pages:2,total:2,has_next:false}})};
};
page.loadMore();await new Promise(setImmediate);await new Promise(setImmediate);
assert.deepEqual(page.visibleListingUpdateIds,[101,102]);assert.deepEqual(page.listingUpdate.selectedIds,[101]);
page.setView('table');assert.equal(page.view,'table');assert.deepEqual(page.listingUpdate.selectedIds,[101]);
page.toggleLoadedListingUpdates(true);assert.deepEqual(page.listingUpdate.selectedIds,[101,102]);
page.toggleListingUpdate(page.items[3],false);assert.deepEqual(page.listingUpdate.selectedIds,[101]);
page.historyRestoring=true;page.filters={...page.filters};definition.watch.filters.handler.call(page);
assert.deepEqual(page.listingUpdate.selectedIds,[101],'same exact account/filter scope survives a history return');
page.listingUpdate.selectedIds=[];page.listingUpdate.selectionScopeKey='';
assert.equal(page.restoreListingUpdateSelection(),true);assert.deepEqual(page.listingUpdate.selectedIds,[101]);
page.filters.status='archived';definition.watch.filters.handler.call(page);
assert.deepEqual(page.listingUpdate.selectedIds,[],'a changed status clears the old set');
page.historyRestoring=false;page.mode='products';
assert.equal(page.listingUpdateScopeEligible,false);assert.equal(page.listingUpdateCanSwitchToListings,true);
page.filters.account_id=null;assert.equal(page.listingUpdateCanSwitchToListings,false,'no selected shop means no switch CTA');
page.filters={marketplace:'wb',account_id:2,status:'',link_status:'',include_unavailable:false,search:''};
page.mode='listings';assert.equal(page.listingUpdateScopeEligible,false,'WB listings cannot use Ozon preparation');

page.filters={marketplace:'ozon',account_id:2,status:'',link_status:'',include_unavailable:false,search:''};
page.mode='listings';page.items=Array.from({length:201},(_,i)=>group(listing(i+1)));
assert.equal(page.toggleLoadedListingUpdates(true),false,'bulk select must refuse, never truncate over 200');
assert.deepEqual(page.listingUpdate.selectedIds,[]);
page.listingUpdate.selectedIds=Array.from({length:200},(_,i)=>i+1);
page.listingUpdate.selectionScopeKey=page.listingUpdateScopeKey();
assert.equal(page.toggleListingUpdate(page.items[200],true),false);
assert.equal(page.listingUpdate.selectedIds.length,200);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_ozon_update_preview_confirmation_and_editor_link_are_exact_local_only():
    run_node(SETUP + r'''
(async()=>{
global.location=new URL('https://seller.test/marketplaces/listings/');
const store=new Map();global.sessionStorage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)};
page.mode='listings';page.filters={marketplace:'ozon',account_id:2,status:'active',link_status:'linked',include_unavailable:false,search:'shoe~blue'};
page.urls={...page.urls,base:'/marketplaces/listings/',previewUpdateDrafts:'/marketplaces/listings/update-drafts/preview',
    applyUpdateDrafts:'/marketplaces/listings/update-drafts/apply',repairDraftSelection:'/marketplaces/ozon/uploads/selection/repair'};
page.catalogReturnUrl='/marketplaces/listings/?marketplace=ozon&account_id=2&status=active&link_status=linked&search=shoe&per_page=1&page=2';
page.perPage=1;page.requestedPage=2;page.loadedStartPage=1;page.loading=false;page.loadingMore=false;
page.listingUpdate.selectedIds=[101,102];
page.listingUpdate.selectionScopeKey=page.listingUpdateScopeKey();
const dialog={open:false,showModal(){this.open=true},close(){this.open=false}};
const trigger={isConnected:true,focus(){this.focused=true}};
const confirmButton={isConnected:true,focus(){this.focused=true}};
const resultHeading={isConnected:true,focus(){this.focused=true}};
page.$refs={listingUpdateDialog:dialog,listingUpdateConfirm:confirmButton,listingUpdateResultHeading:resultHeading};
let previewBody=null,applyBody=null;const calls=[];
const rows=[
    {listing_id:101,title:'Товар 101',offer_id:'offer-101',status:'ready'},
    {listing_id:102,title:'Товар 102',offer_id:'offer-102',status:'existing'},
];
const reportRows=[
    {listing_id:101,title:'Товар 101',offer_id:'offer-101',status:'created',draft_id:701},
    {listing_id:102,title:'Товар 102',offer_id:'offer-102',status:'existing',draft_id:702},
];
global.fetch=async(url,opts)=>{
    calls.push({url,opts});
    if(url==='/marketplaces/listings/update-drafts/preview'){
        previewBody=JSON.parse(opts.body);
        return {ok:true,json:async()=>({success:true,preview:{account_id:2,account_label:'Магазин 2',listing_ids:[101,102],rows,token:'signed-preview-token'}})};
    }
    if(url==='/marketplaces/listings/update-drafts/apply'){
        applyBody=JSON.parse(opts.body);
        const editor=new URL('/marketplaces/ozon/uploads/selection/repair',location.origin);
        editor.searchParams.set('account_id','2');editor.searchParams.set('draft_ids','701,702');
        editor.searchParams.set('return_to',previewBody.return_to.replace(/%7E/g,'~'));
        return {ok:true,json:async()=>({success:true,report:{account_id:2,account_label:'Магазин 2',listing_ids:[101,102],rows:reportRows,editor_url:editor.pathname+editor.search}})};
    }
    throw Error('No other endpoint is allowed');
};
assert.equal(await page.startListingUpdatePreview({currentTarget:trigger}),true);
assert.equal(dialog.open,true);assert.equal(page.listingUpdate.phase,'review');
assert.deepEqual(previewBody.listing_ids,[101,102]);assert.equal(previewBody.account_id,2);
const returnUrl=new URL(previewBody.return_to,location.origin);
assert.equal(returnUrl.pathname,'/marketplaces/listings/');
assert.equal(returnUrl.searchParams.get('per_page'),'1');assert.equal(returnUrl.searchParams.get('page'),'2');
assert.equal(returnUrl.searchParams.get('status'),'active');assert.equal(returnUrl.searchParams.get('link_status'),'linked');
assert.equal(page.listingUpdate.confirmLocal,false);
assert.equal(await page.applyListingUpdatePreview(),false,'the explicit local confirmation checkbox is required');
assert.equal(calls.length,1);
page.listingUpdate.confirmLocal=true;
assert.equal(await page.applyListingUpdatePreview(),true);
assert.deepEqual(applyBody,{token:'signed-preview-token',confirm_local:true});
assert.deepEqual(calls.map(call=>call.url),['/marketplaces/listings/update-drafts/preview','/marketplaces/listings/update-drafts/apply']);
assert.equal(calls.every(call=>call.opts.method==='POST'&&call.opts.credentials==='same-origin'),true);
assert.equal(page.listingUpdate.phase,'complete');assert.equal(page.listingUpdate.report.rows.length,2);
assert.equal(resultHeading.focused,true,'the accepted result receives keyboard focus');
const editorUrl=new URL(page.listingUpdate.editorUrl,location.origin);
assert.equal(editorUrl.pathname,'/marketplaces/ozon/uploads/selection/repair');
assert.equal(editorUrl.searchParams.get('account_id'),'2');assert.equal(editorUrl.searchParams.get('draft_ids'),'701,702');
assert.equal(new URL(editorUrl.searchParams.get('return_to'),location.origin).searchParams.get('per_page'),'1');
assert.equal(page.cancelListingUpdateDialog(),true);assert.equal(dialog.open,false);assert.equal(trigger.focused,true);
page.items=[{key:'l103',listing_count:1,listings:[{id:103,account_id:2,marketplace_code:'ozon'}],
    _p:{id:103,account_id:2,marketplace_code:'ozon'}}];
assert.equal(page.toggleListingUpdate(page.items[0],true),true);
assert.equal(page.listingUpdate.phase,'idle');assert.equal(page.listingUpdate.report,null);
assert.equal(page.listingUpdate.preview,null);assert.deepEqual(page.listingUpdate.selectedIds,[101,102,103]);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_scoped_workspace_update_uses_signed_preview_without_catalog_or_account_roster():
    run_node(r'''
const assert=require('node:assert/strict');global.window=global;
const token='signed.preview.token',returnTo='/my-products/beta#workspace-return=0123456789abcdef0123456789abcdef';
const bootstrap={workspaceUpdateMode:true,workspaceUpdateError:'',ozonEnabled:true,accounts:[],
    filters:{marketplace_code:'ozon',account_id:2},selectionOwner:'7:3',
    urls:{previewUpdateDrafts:'/marketplaces/listings/update-drafts/preview'}};
global.location={href:'https://seller.test/marketplaces/listings/?account_id=2&marketplace=ozon&workspace_update=1#workspace-update='+token,
    origin:'https://seller.test',pathname:'/marketplaces/listings/'};
let definition;const listeners=new Map();let calls=[];let storageCalls=0;
global.document={hidden:false,body:{style:{}},
    getElementById:id=>id==='mcat-bootstrap'?{textContent:JSON.stringify(bootstrap)}:id==='marketplace-catalog-app'?{}:null,
    querySelector:()=>null,addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)};
global.addEventListener=(name,fn)=>listeners.set('window:'+name,fn);
global.removeEventListener=name=>listeners.delete('window:'+name);
global.localStorage={getItem(){storageCalls++;throw Error('scoped handoff must not read preference storage')},setItem(){storageCalls++;throw Error('scoped handoff must not write preference storage')}};
global.sessionStorage={getItem(){storageCalls++;throw Error('scoped handoff must not read cached selection')},setItem(){storageCalls++;throw Error('scoped handoff must not write cached selection')},removeItem(){storageCalls++;throw Error('scoped handoff must not clear cached selection')}};
global.fetch=async(url,options)=>{
    calls.push({url,options});
    return {ok:true,status:200,json:async()=>({success:true,
        workspace_update:{account_id:2,account_label:'Магазин Ozon',listing_ids:[101,102],return_to:returnTo},
        preview:{token:'confirm.preview.token',account_id:2,account_label:'Магазин Ozon',listing_ids:[101,102],
            rows:[{listing_id:101,title:'A',offer_id:'A',status:'ready'},{listing_id:102,title:'B',offer_id:'B',status:'ready'}]}})};
};
global.Vue={createApp:opts=>{definition=opts;return {mount(){}}}};
require('./static/marketplace-beta-shared.js');require('./static/marketplace-catalog-beta.js');
const page=definition.data();Object.entries(definition.methods).forEach(([key,fn])=>page[key]=fn.bind(page));
Object.entries(definition.computed).forEach(([key,fn])=>Object.defineProperty(page,key,{get:fn.bind(page)}));
page.$nextTick=fn=>fn();let opened=false,focusedTarget='';
page.$refs={workspaceUpdateReopen:{focus(){}},listingUpdateDialog:{open:false,showModal(){this.open=true;opened=true},close(){this.open=false}},
    listingUpdateConfirmCheckbox:{isConnected:true,focus(){focusedTarget='checkbox'}},
    listingUpdateReviewHeading:{isConnected:true,focus(){focusedTarget='heading'}}};
page.onKey=()=>{};page.onPopState=()=>{};
(async()=>{
    definition.mounted.call(page);
    await new Promise(setImmediate);await new Promise(setImmediate);
    assert.equal(opened,true,'signed handoff opens the shared review dialog automatically');
    assert.equal(page.workspaceUpdate.accountId,2);assert.deepEqual(page.workspaceUpdate.listingIds,[101,102]);
    assert.equal(page.workspaceUpdate.accountLabel,'Магазин Ozon');assert.equal(page.accounts.length,0);
    assert.equal(page.listingUpdateCanPreview,true);assert.equal(page.workspaceUpdateCanOpen,true);assert.equal(focusedTarget,'checkbox');
    assert.equal(calls.length,1);assert.equal(calls[0].url,'/marketplaces/listings/update-drafts/preview');
    assert.equal(calls[0].options.method,'POST');assert.deepEqual(JSON.parse(calls[0].options.body),{workspace_update:token});
    assert.equal(storageCalls,0);assert.equal(page.items.length,0);assert.deepEqual(page.pagination,{page:1,pages:1,total:0,has_next:false});
    assert.equal(page.workspaceUpdateReturnUrl,returnTo);
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_ozon_update_uncertain_apply_keeps_scope_and_requires_new_read_before_confirmation():
    run_node(SETUP + r'''
(async()=>{
global.location=new URL('https://seller.test/marketplaces/listings/');
page.mode='listings';page.filters={marketplace:'ozon',account_id:2,status:'',link_status:'',include_unavailable:false,search:''};
page.urls={...page.urls,base:'/marketplaces/listings/',previewUpdateDrafts:'/preview',applyUpdateDrafts:'/apply',
    repairDraftSelection:'/marketplaces/ozon/uploads/selection/repair'};
page.catalogReturnUrl='/marketplaces/listings/?marketplace=ozon&account_id=2';
page.listingUpdate.selectedIds=[101];page.listingUpdate.selectionScopeKey=page.listingUpdateScopeKey();page.listingUpdate.confirmLocal=true;
page.$refs={};
let reads=0,applies=0;
global.fetch=async(url)=>{
    if(url==='/preview'){
        reads++;
        return {ok:true,json:async()=>({success:true,preview:{account_id:2,account_label:'Магазин 2',listing_ids:[101],
            rows:[{listing_id:101,title:'Товар 101',offer_id:'offer-101',status:'ready'}],token:'signed'}})};
    }
    applies++;
    return {ok:true,json:async()=>({success:true,report:{account_id:2,account_label:'Магазин 2',listing_ids:[999],
        rows:[{listing_id:999,title:'wrong row',offer_id:'wrong',status:'created',draft_id:901}],editor_url:'/unsafe'}})};
};
assert.equal(await page.readListingUpdatePreview(),true);page.listingUpdate.confirmLocal=true;
assert.equal(await page.applyListingUpdatePreview(),false,'a mismatched outcome cannot be accepted');
assert.equal(page.listingUpdate.phase,'apply_uncertain');assert.deepEqual(page.listingUpdate.selectedIds,[101]);
assert.equal(page.listingUpdate.report,null);assert.equal(page.listingUpdate.editorUrl,null);
assert.equal(reads,1);assert.equal(applies,1);
assert.equal(page.listingUpdateCanApply,false,'no second confirmation is available before a fresh read');
assert.equal(await page.readListingUpdatePreview(),true);assert.equal(reads,2);
assert.equal(page.listingUpdate.confirmLocal,false,'a fresh preview requires a new explicit confirmation');
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_ozon_listing_update_preview_and_apply_timeouts_are_bounded_and_recoverable():
    run_node(SETUP + r'''
(async()=>{
global.location=new URL('https://seller.test/marketplaces/listings/');
page.mode='listings';page.filters={marketplace:'ozon',account_id:2,status:'',link_status:'',include_unavailable:false,search:''};
page.urls={...page.urls,base:'/marketplaces/listings/',previewUpdateDrafts:'/preview',applyUpdateDrafts:'/apply',
    repairDraftSelection:'/marketplaces/ozon/uploads/selection/repair'};
page.catalogReturnUrl='/marketplaces/listings/?marketplace=ozon&account_id=2';
page.perPage=60;page.requestedPage=1;page.loadedStartPage=1;page.loading=false;page.loadingMore=false;
page.listingUpdate.selectedIds=[101];page.listingUpdate.selectionScopeKey=page.listingUpdateScopeKey();
const dialog={open:true,close(){this.open=false}};page.$refs={listingUpdateDialog:dialog};
const timers=new Map();let nextTimer=1;
global.setTimeout=(callback,ms)=>{const id=nextTimer++;timers.set(id,{callback,ms});return id};
global.clearTimeout=id=>{timers.delete(id)};
const fireTimer=ms=>{
    const entry=Array.from(timers.entries()).find(([,timer])=>timer.ms===ms);
    assert.ok(entry,'expected a '+ms+'ms deadline');timers.delete(entry[0]);entry[1].callback();
};
const abortError=()=>{const error=new Error('aborted');error.name='AbortError';return error};
const pendingUntilAbort=signal=>new Promise((resolve,reject)=>{
    if(signal.aborted){reject(abortError());return}
    signal.addEventListener('abort',()=>reject(abortError()),{once:true});
});
const validPreview=(token)=>({success:true,preview:{account_id:2,account_label:'Магазин 2',listing_ids:[101],
    rows:[{listing_id:101,title:'Товар 101',offer_id:'offer-101',status:'ready'}],token}});
let mode='preview_fetch',reads=0,applies=0;
global.fetch=async(url,options)=>{
    if(url==='/preview'){
        reads++;
        if(mode==='preview_fetch')return pendingUntilAbort(options.signal);
        if(mode==='preview_body')return {ok:true,json:()=>pendingUntilAbort(options.signal)};
        return {ok:true,json:async()=>validPreview('preview-'+reads)};
    }
    if(url==='/apply'){
        applies++;
        if(mode==='apply_body')return {ok:true,json:()=>pendingUntilAbort(options.signal)};
        if(mode==='apply_fetch')return pendingUntilAbort(options.signal);
        throw Error('unexpected apply response mode');
    }
    throw Error('unexpected URL '+url);
};

let pending=page.readListingUpdatePreview();
assert.equal(page.listingUpdate.phase,'previewing');assert.equal(timers.size,1);
assert.equal(Array.from(timers.values())[0].ms,15000);fireTimer(15000);
assert.equal(await pending,false);assert.equal(page.listingUpdate.phase,'read_failed');
assert.match(page.listingUpdate.error,/слишком много времени/);assert.deepEqual(page.listingUpdate.selectedIds,[101]);
assert.equal(page.listingUpdateCanPreview,true);assert.equal(timers.size,0);

mode='preview_body';pending=page.readListingUpdatePreview();
assert.equal(timers.size,1);assert.equal(Array.from(timers.values())[0].ms,15000);fireTimer(15000);
assert.equal(await pending,false);assert.equal(page.listingUpdate.phase,'read_failed');
assert.match(page.listingUpdate.error,/слишком много времени/);assert.equal(timers.size,0);

mode='preview_ok';assert.equal(await page.readListingUpdatePreview(),true);
assert.equal(page.listingUpdate.phase,'review');assert.equal(timers.size,0);
page.listingUpdate.confirmLocal=true;mode='apply_body';pending=page.applyListingUpdatePreview();
assert.equal(page.listingUpdate.phase,'applying');assert.equal(timers.size,1);
assert.equal(Array.from(timers.values())[0].ms,45000);assert.equal(page.cancelListingUpdateDialog(),false);
fireTimer(45000);assert.equal(await pending,false);assert.equal(page.listingUpdate.phase,'apply_uncertain');
assert.match(page.listingUpdate.error,/45 секунд.*Результат неизвестен/);assert.equal(timers.size,0);
assert.equal(page.listingUpdateCanPreview,true);assert.equal(applies,1);
assert.equal(page.cancelListingUpdateDialog(),true,'the timed out confirmation can now be closed');

dialog.open=true;mode='preview_ok';assert.equal(await page.readListingUpdatePreview(),true);
assert.equal(page.listingUpdate.phase,'review');assert.equal(page.listingUpdate.confirmLocal,false);
assert.equal(applies,1,'a fresh read does not automatically repeat the uncertain apply');assert.equal(timers.size,0);
page.listingUpdate.confirmLocal=true;mode='apply_fetch';pending=page.applyListingUpdatePreview();
assert.equal(page.listingUpdate.phase,'applying');assert.equal(timers.size,1);fireTimer(45000);
assert.equal(await pending,false);assert.equal(page.listingUpdate.phase,'apply_uncertain');
assert.equal(applies,2);assert.equal(timers.size,0);

dialog.open=true;mode='preview_fetch';pending=page.readListingUpdatePreview();
assert.equal(timers.size,1);assert.equal(page.cancelListingUpdateDialog(),true);
assert.equal(timers.size,0);assert.equal(page.listingUpdate.phase,'idle');
assert.equal(await pending,false);assert.equal(page.listingUpdate.phase,'idle','a manual cancel stays silent');
assert.deepEqual(page.listingUpdate.selectedIds,[101]);assert.equal(timers.size,0);
})().catch(e=>{console.error(e);process.exitCode=1});
''')
