"""Browser-side async behavior without a live Ozon key or network."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]


def test_one_post_visible_polling_completion_and_period_race():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for JavaScript behavior checks')
    source = (ROOT / 'static/ozon-read-refresh.js').read_text()
    script = r'''
const assert = require('node:assert/strict');
global.window=global;
const listeners=new Map(); const timers=new Map(); let nextTimer=0;
global.document={hidden:false,addEventListener:(k,v)=>listeners.set(k,v),removeEventListener:k=>listeners.delete(k)};
global.setTimeout=(f,ms)=>{timers.set(++nextTimer,{f,ms});return nextTimer;};
global.clearTimeout=id=>timers.delete(id);
''' + source + r'''
(async()=>{
  let requests=[]; let reloads=0; let successes=0;
  global.fetch=(url,options={})=>new Promise(resolve=>requests.push({url,options,resolve}));
  const page={...ozonReadRefresh({accountId:17,domain:'analytics',csrfToken:'synthetic'}),
    period:'30d',onRefreshCompleted:async()=>reloads++,$store:{toasts:{success:()=>successes++}}};
  const reply=(n,state)=>requests[n].resolve({ok:true,json:async()=>({data:{account_id:17,domain:'analytics',...state}})});
  const pending={id:1,status:'pending',active:true,period:'30d'};
  const start=page.requestRefresh();
  assert.equal(requests.length,1);assert.equal(requests[0].options.method,'POST');
  await page.requestRefresh();assert.equal(requests.length,1);
  reply(0,pending);await start;
  assert.equal(successes,0);assert.equal(reloads,0);assert.equal(timers.size,1);
  const poll=page.loadRefreshStatus();assert.equal(requests[1].options.method,undefined);
  reply(1,{...pending,status:'waiting',next_attempt_at:new Date(Date.now()+5000000).toISOString()});await poll;
  assert.equal(successes,0);assert.equal([...timers.values()][0].ms,60000);
  document.hidden=true;await page.loadRefreshStatus();assert.equal(requests.length,2);
  page.scheduleRefreshPoll();assert.equal(timers.size,0);
  document.hidden=false;
  const finish=page.loadRefreshStatus();reply(2,{...pending,status:'completed',active:false});await finish;
  assert.equal(successes,1);assert.equal(reloads,1);assert.equal(timers.size,0);
  const repeat=page.loadRefreshStatus();reply(3,{...pending,status:'completed',active:false});await repeat;
  assert.equal(successes,1);assert.equal(reloads,1);
  const old=page.loadRefreshStatus();page.period='7d';const changed=page.changeRefreshPeriod();
  reply(5,{id:2,status:'pending',active:true,period:'7d'});await changed;
  reply(4,{...pending,status:'completed',active:false});await old;
  assert.equal(page.refreshState.id,2);assert.equal(successes,1);
  const abandoned=page.loadRefreshStatus();page.destroyRefresh();
  reply(6,{id:2,status:'completed',active:false,period:'7d'});await abandoned;
  assert.equal(successes,1);assert.equal(timers.size,0);
  // An expired CSRF/session can return HTML; never expose a JSON parser error,
  // infer success, or send a second POST while recovering its status.
  const recovery={...ozonReadRefresh({accountId:17,domain:'analytics',csrfToken:'synthetic'}),
    period:'7d',onRefreshCompleted:async()=>reloads++};
  const methods=[];
  global.fetch=async(url,options={})=>{
    methods.push(options.method||'GET');
    return options.method==='POST'
      ? {ok:false,json:async()=>{throw new SyntaxError('Unexpected token <');}}
      : {ok:true,json:async()=>({data:{account_id:17,domain:'analytics',id:3,status:'pending',active:true,period:'7d'}})};
  };
  await recovery.requestRefresh();assert.equal(recovery.refreshing,true);
  assert.equal(recovery.refreshUncertain,true);
  await recovery.requestRefresh();assert.deepEqual(methods,['POST']);
  assert.ok(recovery.refreshStatusError.includes('Обновите страницу'));
  assert.ok(!recovery.refreshStatusError.includes('Unexpected'));
  await recovery.initRefresh();assert.equal(recovery.refreshState.id,3);
  document.hidden=true;listeners.get('visibilitychange')();assert.equal(timers.size,0);
  recovery.destroyRefresh();assert.equal(listeners.size,0);
  assert.deepEqual(methods,['POST','GET']);assert.equal(successes,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
