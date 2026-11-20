"""Cooldown UI contract for the shared Ozon account/catalog controls."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]


def run_node(body):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for JavaScript behavior checks')
    setup = r'''
const assert=require('node:assert/strict');global.window=global;
global.location=new URL('https://fixture.test/marketplaces/accounts/?account_id=7');
const timers=new Map();let nextTimer=0;
global.setTimeout=(fn,ms)=>{timers.set(++nextTimer,{fn,ms});return nextTimer};
global.clearTimeout=id=>timers.delete(id);
global.document={hidden:false,addEventListener(){},removeEventListener(){},querySelector(){return null},getElementById(){return null}};
let formDataCalls=0;
global.FormData=class {constructor(form){formDataCalls++;this.form=form}get(){return null}};
require('./static/ozon-account-setup.js');
const controller=window.ozonAccountSetup();const page={};
for(const [key,value] of Object.entries(controller)) page[key]=typeof value==='function'?value.bind(page):value;
page.config={status_url:'/marketplaces/accounts/api',accounts_url:'/marketplaces/accounts'};
page.accounts[7]={id:7,marketplace_code:'ozon',external_account_id:'synthetic',version:2,is_active:true,has_credentials:true,connection_status:'connected'};
function reply(data,status=200){return {ok:status>=200&&status<300,status,redirected:false,headers:{get:()=> 'application/json'},json:async()=>data}}
function syncForm({manualCooldown=true,catalogSync=true,action='/marketplaces/listings/accounts/7/sync'}={}){
  const dataset={accountId:'7'};if(manualCooldown)dataset.manualCooldown='true';if(catalogSync)dataset.catalogSync='true';
  return {reportValidity:()=>true,dataset,action:new URL(action,location.href).href,elements:{namedItem:()=>({value:'csrf'})}};
}
'''
    script = setup + '\n(async()=>{' + body + '\n})().catch(e=>{console.error(e);process.exitCode=1});'
    completed = subprocess.run(
        [node, '-e', script], cwd=ROOT, text=True,
        capture_output=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_exact_failed_cooldown_blocks_both_stored_key_posts_and_allows_status_get():
    run_node(r'''
const future=new Date(Date.now()+60*60*1000).toISOString();
page.jobs[7]={status:'failed',manual_retry_at:future,next_retry_at:'2099-01-01T00:00:00Z'};
assert.equal(page.cooldownAt(7),future);assert.equal(page.coolingDown(7),true);
assert.match(page.formattedCooldownAt(7),/UTC$/);assert.equal(page.retryTime(7),'');
let posts=0,gets=0;global.fetch=async(url,options={})=>{if(options.method==='POST')posts++;else gets++;throw new Error('unexpected fetch')};
await page.submit({currentTarget:syncForm({manualCooldown:true,catalogSync:true})});
await page.submit({currentTarget:syncForm({manualCooldown:true,catalogSync:false,action:'/marketplaces/accounts/7/connect'})});
assert.equal(posts,0);assert.equal(formDataCalls,0,'cooldown guard runs before form serialization');

global.fetch=async(url,options={})=>{
  assert.equal(url,'/marketplaces/accounts/api');assert.equal(options.method,undefined);
  gets++;
  return reply({success:true,accounts:[page.accounts[7]],onboarding_jobs:{7:{status:'failed',manual_retry_at:future}},catalog_syncs:{},catalog_completed_at:{},ozon_enabled:true});
};
await page.refresh();
assert.equal(gets,1,'status recheck is a local GET');assert.equal(posts,0);assert.equal(page.jobs[7].status,'failed');
''')


def test_past_invalid_and_unverified_cutoffs_leave_attempts_eligible():
    run_node(r'''
let posts=0;global.fetch=async(url,options)=>{
  assert.equal(options.method,'POST');posts++;
  return reply({success:true,message:'Catalog request accepted',job:{account_id:7,status:'pending',message:'Queued'}},202);
};
for(const manualRetryAt of ['invalid-deadline',new Date(Date.now()-5000).toISOString(),null]){
  page.jobs[7]={status:'failed',manual_retry_at:manualRetryAt};
  assert.equal(page.cooldownAt(7),null);assert.equal(page.coolingDown(7),false);
  await page.submit({currentTarget:syncForm({manualCooldown:true,catalogSync:true})});
  assert.equal(page.jobs[7].status,'pending');assert.equal(page.errors[7]||'','');
}
assert.equal(posts,3,'eligible catalog POSTs are still sent and exact job scope is confirmed');
''')


def test_active_deadline_and_long_deadline_wake_do_not_create_mutations():
    run_node(r'''
const future=new Date(Date.now()+60*1000).toISOString();
page.jobs[7]={status:'pending',manual_retry_at:future,next_retry_at:future};
assert.equal(page.coolingDown(7),false);assert.ok(page.retryTime(7).startsWith('Следующая попытка: '));
page.jobs[7]={status:'failed',manual_retry_at:'9999-12-31T23:59:59Z'};
page.scheduleCooldownWake();
assert.ok([...timers.values()].some(timer=>timer.ms===2147483647),'long deadlines are clamped to the largest safe timer');
assert.equal(page.coolingDown(7),true);assert.equal(page.jobs[7].status,'failed');
page.destroy();assert.equal(timers.size,0);
''')


def test_catalog_202_confirms_exact_job_without_changing_credential_response_check():
    run_node(r'''
page.jobs[7]={status:'failed',manual_retry_at:new Date(Date.now()-5000).toISOString()};
const existingAccount=page.accounts[7];
global.fetch=async(url,options)=>{
  assert.equal(options.method,'POST');
  return reply({success:true,message:'Catalog request accepted',
    account:{id:7,marketplace_code:'ozon',external_account_id:'synthetic',version:99,label:'Stray response account'},
    job:{account_id:7,status:'pending',message:'Queued'}},202);
};
await page.submit({currentTarget:syncForm({manualCooldown:true,catalogSync:true})});
assert.equal(page.jobs[7].status,'pending');assert.equal(page.errors[7]||'','');
assert.equal(page.accounts[7],existingAccount,'job-only catalog 202 cannot replace account bootstrap data');

// Credential forms do not use the catalog cooldown marker and retain the
// existing exact account response validation path.
let credentialPosts=0;global.fetch=async()=>{credentialPosts++;throw new TypeError('synthetic network error')};
await page.submit({currentTarget:syncForm({manualCooldown:false,catalogSync:false,action:'/marketplaces/accounts/7/reconnect'})});
assert.equal(credentialPosts,1);assert.ok(page.errors[7].includes('Ответ не получен'));
''')


def test_cooldown_conflict_reloads_status_once_without_replaying_catalog_post():
    run_node(r'''
const future=new Date(Date.now()+60*60*1000).toISOString();let posts=0,gets=0;
global.fetch=async(url,options={})=>{
  if(options.method==='POST'){
    posts++;return reply({success:false,code:'ozon_catalog_cooldown',error:'Озон запросил паузу'},409);
  }
  gets++;assert.equal(url,'/marketplaces/accounts/api');
  return reply({success:true,accounts:[page.accounts[7]],onboarding_jobs:{7:{status:'failed',manual_retry_at:future}},catalog_syncs:{},catalog_completed_at:{},ozon_enabled:true});
};
await page.submit({currentTarget:syncForm({manualCooldown:true,catalogSync:true})});
assert.equal(posts,1);assert.equal(gets,1);assert.equal(page.cooldownAt(7),future);
assert.equal(page.errors[7],'');assert.equal(page.pollError,'');
''')


def test_failed_conflict_status_read_keeps_auth_and_action_error():
    run_node(r'''
let posts=0,gets=0;global.fetch=async(url,options={})=>{
  if(options.method==='POST'){
    posts++;return reply({success:false,code:'ozon_catalog_cooldown',error:'Озон запросил паузу'},409);
  }
  gets++;return reply({success:false,error:'Session expired'},401);
};
await page.submit({currentTarget:syncForm({manualCooldown:true,catalogSync:true})});
assert.equal(posts,1);assert.equal(gets,1);assert.equal(page.coolingDown(7),false);
assert.equal(page.sessionExpired,true);assert.match(page.pollError,/Сессия истекла/);
assert.match(page.errors[7],/Озон запросил паузу/);
''')
