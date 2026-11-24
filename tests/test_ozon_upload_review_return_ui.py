"""Scoped Ozon review returns stay exact without changing legacy review requests."""
from pathlib import Path
import subprocess


def test_scoped_workspace_return_and_compact_review_navigation():
    helper = Path(__file__).parents[1] / "static/ozon-workspace-return.js"
    review_script = Path(__file__).parents[1] / "static/ozon-upload-review.js"
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const helperSource=fs.readFileSync(process.argv[1],'utf8');
const reviewSource=fs.readFileSync(process.argv[2],'utf8');
const storageMap=new Map();
const sourceHref='/my-products/beta?import_batch=alpha.Beta_9.gamma#workspace-return=0123456789abcdef0123456789abcdef';
const ids=Array.from({length:21},(_,index)=>index+101);
const tokenFor=ids=>{
  const bytes=[];
  for(const value of ids){let id=BigInt(value),offset=bytes.length;for(let index=7;index>=0;index--){bytes[offset+index]=Number(id&255n);id>>=8n;}}
  return Buffer.from(bytes).toString('base64url');
};
const link={href:'/my-products/beta',querySelector(){return this.label;},label:{textContent:'К моим товарам'}};
const storage={getItem(key){return storageMap.has(key)?storageMap.get(key):null;},setItem(key,value){storageMap.set(key,value);},removeItem(key){storageMap.delete(key);}};
function createContext({review,href,anchor=true,fetchImpl=async()=>{throw Error('Unexpected fetch')}}){
  link.href='/my-products/beta';link.label.textContent='К моим товарам';
  const app={options:null};
  const window={location:{href,origin:'https://app.test'},sessionStorage:storage,
    btoa:value=>Buffer.from(value,'binary').toString('base64'),atob:value=>Buffer.from(value,'base64').toString('binary'),
    Vue:{createApp(options){app.options=options;return {component(){return this;},mount(){}};}},
    ozonDraftEditor:{photoComponent:{},isNavigableIssue(){return false;},safeImage(value){return value;}}};
  const bootstrap={textContent:JSON.stringify({review,urls:{html:'/marketplaces/ozon/uploads/review',api:'/marketplaces/ozon/uploads/api/review',
    publish:'/publish',byRequest:'/by-request',runBase:'/run/',editorBase:'/draft/'}})};
  const document={getElementById(id){return id==='our-bootstrap'?bootstrap:null;},
    querySelector(selector){return anchor&&selector==='[data-review-workspace-return]'?link:null;}};
  const context={window,document,sessionStorage:storage,URL,URLSearchParams,TextEncoder,TextDecoder,AbortController,
    setTimeout,clearTimeout,fetch:fetchImpl};
  vm.runInNewContext(helperSource,context);
  vm.runInNewContext(reviewSource,context);
  return {app,window,bootstrap,document};
}
const scoped={account_id:9,account_label:'Магазин 9',draft_ids:ids,workspace_selection:tokenFor(ids),workspace_owner:'41:72',
  parent_prepare_job_uid:null,result_job_uid:null,pagination:{page:1,pages:2,total:21,per_page:20},items:[],checked_at:'2026-11-22T00:00:00Z',
  publication_enabled:true,csrf_token:'csrf'};
(async()=>{
const fragment=(()=>{
  const bytes=new TextEncoder().encode(sourceHref),encoded=Buffer.from(bytes).toString('base64url');
  return '#workspace-return='+encoded;
})();
const first=createContext({review:scoped,href:'https://app.test/marketplaces/ozon/uploads/review?account_id=9&workspace_selection='+scoped.workspace_selection+fragment});
assert.equal(link.href,sourceHref,'validated source capsule upgrades the safe fallback link');
assert.equal(link.label.textContent,'К выбранным товарам','exact return has a clear label');
const options=first.app.options,appData=options.data();
const pageTwo=new URL(options.methods.pageUrl.call(appData,2),'https://app.test');
assert.equal(pageTwo.searchParams.get('workspace_selection'),scoped.workspace_selection,'pagination keeps canonical compact scope');
assert.equal(pageTwo.searchParams.has('draft_ids'),false,'scoped pagination never expands IDs');
assert.equal(pageTwo.searchParams.get('page'),'2');
assert.equal(pageTwo.hash,fragment,'pagination carries the source capsule');

let apiUrl='';
const freshRow={draft_id:ids[20],version:3,selectable:true,action:'update'};
const apiReview={...scoped,draft_ids:[ids[20]],workspace_selection:tokenFor([ids[20]]),pagination:{page:1,pages:1,total:1,per_page:20},items:[freshRow]};
const checking=createContext({review:scoped,href:'https://app.test/marketplaces/ozon/uploads/review?account_id=9&workspace_selection='+scoped.workspace_selection+fragment,
  fetchImpl:async url=>{apiUrl=url;return {status:200,ok:true,redirected:false,headers:{get:()=> 'application/json'},json:async()=>({success:true,review:apiReview})};}});
const exact=await checking.app.options.methods.readExactReview.call(checking.app.options.data(),[ids[20]]);
assert.equal(exact.rows[0].draft_id,ids[20]);
const apiQuery=new URL(apiUrl,'https://app.test').searchParams;
assert.equal(apiQuery.get('workspace_selection'),tokenFor([ids[20]]),'selected-version recheck remains compact');
assert.equal(apiQuery.has('draft_ids'),false,'selected-version recheck never uses expanded IDs');

const restored=createContext({review:scoped,href:'https://app.test/marketplaces/ozon/uploads/review?account_id=9&workspace_selection='+scoped.workspace_selection});
assert.equal(link.href,sourceHref,'signed issue-to-review return restores the same-tab capsule by exact scope');
assert.equal(link.label.textContent,'К выбранным товарам');
const restoredPage=new URL(restored.app.options.methods.pageUrl.call(restored.app.options.data(),2),'https://app.test');
assert.equal(restoredPage.hash,fragment,'restored capsule survives the next page hop');

const helper=first.window.OzonWorkspaceReturn, exactScope=helper.reviewWorkspaceScope(scoped), boundedStore=new Map();
const boundedStorage={getItem(key){return boundedStore.has(key)?boundedStore.get(key):null;},
  setItem(key,value){boundedStore.set(key,value);},removeItem(key){boundedStore.delete(key);}};
const fixedNow=1000;
assert.equal(helper.storeReviewWorkspaceReturn(exactScope,sourceHref,{storage:boundedStorage,now:()=>fixedNow,origin:'https://app.test'}),sourceHref);
const storedRecord=JSON.parse(boundedStore.get(helper.SESSION_KEY));
assert.equal(storedRecord.expires_at-fixedNow,helper.REVIEW_RETURN_TTL_MS,'stored capsule expires within 30 minutes');
assert.equal(helper.readReviewWorkspaceReturn(exactScope,{storage:boundedStorage,now:()=>fixedNow+helper.REVIEW_RETURN_TTL_MS-1,origin:'https://app.test'}),sourceHref);
assert.equal(helper.readReviewWorkspaceReturn(exactScope,{storage:boundedStorage,now:()=>fixedNow+helper.REVIEW_RETURN_TTL_MS,origin:'https://app.test'}),null,'expired capsule is unavailable');
for(const altered of [
  {...scoped,workspace_owner:'42:72'},
  {...scoped,account_id:10},
  {...scoped,draft_ids:ids.slice().reverse(),workspace_selection:tokenFor(ids.slice().reverse())},
]){
  const alteredScope=helper.reviewWorkspaceScope(altered);
  assert(alteredScope);
  helper.storeReviewWorkspaceReturn(exactScope,sourceHref,{storage:boundedStorage,now:()=>fixedNow,origin:'https://app.test'});
  assert.equal(helper.readReviewWorkspaceReturn(alteredScope,{storage:boundedStorage,now:()=>fixedNow+1,origin:'https://app.test'}),null,
    'session return is bound to owner, account, and ordered draft IDs');
}

const legacy={...scoped};delete legacy.workspace_selection;delete legacy.workspace_owner;
const legacyContext=createContext({review:legacy,href:'https://app.test/marketplaces/ozon/uploads/review?account_id=9&draft_ids='+ids.join(',')});
const legacyPage=new URL(legacyContext.app.options.methods.pageUrl.call(legacyContext.app.options.data(),2),'https://app.test');
assert.equal(legacyPage.searchParams.get('draft_ids'),ids.join(','),'legacy review keeps its existing query format');
assert.equal(legacyPage.searchParams.has('workspace_selection'),false);

storageMap.set('seller-hub-ozon-review-workspace-return-v1','{broken');
const corrupt=createContext({review:scoped,href:'https://app.test/marketplaces/ozon/uploads/review?account_id=9&workspace_selection='+scoped.workspace_selection});
assert.equal(link.href,'/my-products/beta','corrupt storage leaves the same-origin safe fallback');
assert.equal(link.label.textContent,'К моим товарам','fallback text does not claim an exact return');
assert.equal(storageMap.has('seller-hub-ozon-review-workspace-return-v1'),false,'corrupt entry is discarded');
console.log('scoped Ozon review workspace return and compact navigation: passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(
        ["node", "-e", script, str(helper), str(review_script)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    template = (Path(__file__).parents[1] / "templates/ozon_upload_review.html").read_text()
    assert "data-review-workspace-return href=\"{{ url_for('seller_my_products_beta') }}\"" in template
    assert "data-review-workspace-return-label>К моим товарам" in template
    assert "workspace_selection=review.workspace_selection" in template
    assert "filename='ozon-workspace-return.js', v='20261122-ozon-workspace-review-return-r57'" in template
    assert "filename='ozon-upload-review.js', v='20261124-linked-price-context-r1'" in template
    assert "filename='ozon-upload-review.css', v='20261124-linked-price-context-r2'" in template
    assert template.count('class="our-cleanup-note"') >= 4
    noscript = template.split("<noscript>", 1)[1].split("</noscript>", 1)[0]
    assert "row.automatic_attribute_cleanup.attribute_name" in noscript
    assert "row.automatic_attribute_cleanup.before_count" in noscript
    assert "категория {{ row.external_category_id }}" not in template
    assert "товар № {{ row.imported_product_id }}" not in template
    from flask import Flask
    app = Flask(__name__, template_folder=str(Path(__file__).parents[1] / "templates"))
    assert app.jinja_env.get_template("ozon_upload_review.html") is not None
