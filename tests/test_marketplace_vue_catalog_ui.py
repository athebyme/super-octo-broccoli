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
