"""Status reads preserve the result page and stop outside an active visible job."""
import shutil
import subprocess

import pytest


def test_result_polling_completion_hidden_session_and_late_response():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for status behavior check')
    script = r'''
const assert=require('node:assert/strict');global.window=global;
const timers=new Map();let sequence=0;
global.setTimeout=(fn,delay)=>{const id=++sequence;timers.set(id,{fn,delay});return id};
global.clearTimeout=id=>timers.delete(id);
const listeners={};global.document={hidden:false,getElementById:()=>null,addEventListener:(k,v)=>listeners[k]=v,removeEventListener:k=>delete listeners[k]};
global.location={reload:()=>assert.fail('must preserve document'),assign:()=>assert.fail('must preserve input')};
require('./static/ozon-result-status.js');
const initial={status:'submitted',version:1,next_poll_at:'2026-09-24T20:00:00Z'};
function make(){const options=ozonResultStatus.createOptions({kind:'operation',initial,url:'/result'}),page=options.data();for(const [k,f]of Object.entries(options.methods))page[k]=f.bind(page);for(const [k,f]of Object.entries(options.computed))Object.defineProperty(page,k,{get:f.bind(page)});return {page,options};}
const response=(value,status=200)=>({status,ok:status===200,headers:{get:()=> 'application/json'},json:async()=>({success:true,operation:value})});
(async()=>{
 let {page,options}=make();options.mounted.call(page);assert.equal([...timers.values()][0].delay,5000);
 document.hidden=true;listeners.visibilitychange();assert.equal(timers.size,0);
 let reads=0;global.fetch=async()=>{reads++;return response({...initial,status:'succeeded',version:2,next_poll_at:null})};
 await page.refresh();assert.equal(reads,0);document.hidden=false;listeners.visibilitychange();assert.equal(timers.size,1);
 await page.refresh();assert.equal(reads,1);assert.equal(page.label,'Выполнено');assert.equal(page.changed,true);assert.equal(timers.size,0);options.beforeUnmount.call(page);
 ({page,options}=make());options.mounted.call(page);global.fetch=async()=>response(null,401);await page.refresh();assert.match(page.error,/Войдите/);assert.equal(timers.size,0);options.beforeUnmount.call(page);
 ({page,options}=make());options.mounted.call(page);let finish;global.fetch=()=>new Promise(resolve=>finish=resolve);const running=page.refresh();options.beforeUnmount.call(page);finish(response({...initial,status:'succeeded',version:2}));await running;assert.equal(page.current.status,'submitted');assert.equal(timers.size,0);
 ({page,options}=make());page.current={...initial,status:'uncertain',next_poll_at:null,request_summary:{provider_read_not_before:'9999-12-31T23:59:59.999999'}};options.mounted.call(page);assert.equal(page.automaticStopped,true);assert.match(page.cooldownLabel,/слишком долгую паузу/);assert.equal(timers.size,0);
 page.current.request_summary.provider_read_not_before=new Date(Date.now()+7200000).toISOString().replace('Z','');assert.match(page.cooldownLabel,/не раньше/);assert.match(page.cooldownLabel,/ваше время/);
 page.current.status='succeeded';assert.equal(page.cooldownLabel,'');assert.equal(page.automaticStopped,false);options.beforeUnmount.call(page);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
