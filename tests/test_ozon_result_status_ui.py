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


def test_source_prepare_terminal_region_swap_is_scoped_and_defers_for_focus():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for status behavior check')
    script = r'''
const assert=require('node:assert/strict');global.window=global;
const timers=new Map();let timerId=0;
global.setTimeout=(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id};
global.clearTimeout=id=>timers.delete(id);
function matchesCompound(node, selector){
 if(!node||!node.tagName)return false;
 const tag=selector.match(/^[a-zA-Z][\w-]*/)?.[0];
 if(tag&&node.tagName.toLowerCase()!==tag.toLowerCase())return false;
 for(const match of selector.matchAll(/\[([^\]=]+)(?:="([^"]*)")?\]/g)){
  if(!node.hasAttribute(match[1]))return false;
  if(match[2]!==undefined&&node.getAttribute(match[1])!==match[2])return false;
 }
 for(const match of selector.matchAll(/\.([a-zA-Z0-9_-]+)/g)){
  if(!(node.getAttribute('class')||'').split(/\s+/).includes(match[1]))return false;
 }
 return selector==='*'||!!tag||selector.includes('[')||selector.includes('.');
}
function matchesSelector(node, selector){
 return selector.split(',').some(group=>{
  const parts=group.trim().split(/\s+/);let current=node;
  if(!matchesCompound(current,parts.pop()))return false;
  while(parts.length){const wanted=parts.pop();current=current.parentNode;
   while(current&&!matchesCompound(current,wanted))current=current.parentNode;
   if(!current)return false;
  }
  return true;
 });
}
class Element{
 constructor(tag,attrs={},children=[]){this.tagName=tag.toUpperCase();this.map=new Map(Object.entries(attrs));this.children=[];this.parentNode=null;this.open=false;children.forEach(child=>this.appendChild(child));}
 get attributes(){return Array.from(this.map,([name,value])=>({name,value}));}
 getAttribute(name){return this.map.has(name)?this.map.get(name):null;}
 setAttribute(name,value){this.map.set(name,String(value));}
 hasAttribute(name){return this.map.has(name);}
 appendChild(child){if(child.parentNode)child.remove();this.children.push(child);child.parentNode=this;return child;}
 remove(){if(!this.parentNode)return;const index=this.parentNode.children.indexOf(this);if(index>=0)this.parentNode.children.splice(index,1);this.parentNode=null;}
 replaceWith(node){const parent=this.parentNode;if(!parent)throw Error('missing parent');if(node.parentNode)node.remove();const index=parent.children.indexOf(this);parent.children[index]=node;node.parentNode=parent;this.parentNode=null;}
 contains(node){return node===this||this.children.some(child=>child.contains(node));}
 matches(selector){return matchesSelector(this,selector);}
 querySelectorAll(selector){const found=[];const visit=node=>node.children.forEach(child=>{if(matchesSelector(child,selector))found.push(child);visit(child);});visit(this);return found;}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 closest(selector){let node=this;while(node){if(matchesSelector(node,selector))return node;node=node.parentNode;}return null;}
}
const jobUid='ozon-upload-'+'a'.repeat(32),accountId=17;
function makeRegions(complete,productHref,journeyOpen){
 const header=new Element('div',{'data-source-prepare-region':'header','data-source-prepare-job':jobUid,'data-source-prepare-account':String(accountId),'data-source-prepare-complete':complete});
 const slot=new Element('span',{'data-source-prepare-products-slot':''});
 const product=new Element('a',{'data-source-prepare-products':'','href':productHref});slot.appendChild(product);header.appendChild(slot);
 const body=new Element('div',{'data-source-prepare-region':'body','data-source-prepare-job':jobUid,'data-source-prepare-account':String(accountId),'data-source-prepare-complete':complete});
 const journey=new Element('details',{'class':'preparation-journey-path'});journey.open=journeyOpen;
 const action=new Element('a',{'data-action':'existing-result-action'});body.appendChild(journey);body.appendChild(action);
 return {header,body,product,journey,action};
}
function makeDocument(complete='0'){
 const root=new Element('main');const regions=makeRegions(complete,'/my-products/beta#workspace-return='+'b'.repeat(32),true);
 root.appendChild(regions.header);root.appendChild(regions.body);
 const status=new Element('div');const fallback=new Element('a',{'data-source-prepare-refresh-fallback':''});status.appendChild(fallback);root.appendChild(status);
 const listeners={};const doc={hidden:false,activeElement:null,root,listeners,
  querySelectorAll:selector=>root.querySelectorAll(selector),querySelector:selector=>root.querySelector(selector),
  getElementById:()=>null,addEventListener:(name,fn)=>listeners[name]=fn,removeEventListener:name=>delete listeners[name],
  createElement(tag){assert.equal(tag,'template');const template={content:new Element('template-content'),set innerHTML(value){assert.equal(value,'terminal-page');const fresh=makeRegions('1','/my-products/beta',false);this.content.appendChild(fresh.header);this.content.appendChild(fresh.body);}};return template;}};
 return {doc,regions};
}
let state=makeDocument('0');global.document=state.doc;
global.location={href:'https://seller.test/marketplaces/ozon/uploads/ozon-upload-'+'a'.repeat(32),origin:'https://seller.test',
 pathname:'/marketplaces/ozon/uploads/ozon-upload-'+'a'.repeat(32),search:'',reload:()=>assert.fail('no document reload'),assign:()=>assert.fail('no navigation')};
require('./static/ozon-result-status.js');
const initial={job_uid:jobUid,account_id:accountId,mode:'source_prepare',status:'running',source_prepare_complete:false,
 summary:{version:3,mode:'source_prepare',job_uid:jobUid,account_id:accountId,total:2,processed:1,active:1,outcome:'running'}};
function optionsFor(){
 const options=ozonResultStatus.createOptions({kind:'run',initial,url:'/marketplaces/ozon/uploads/api/'+jobUid,terminal_html_url:'/marketplaces/ozon/uploads/'+jobUid});
 const page=options.data();for(const [key,fn]of Object.entries(options.methods))page[key]=fn.bind(page);
 for(const [key,fn]of Object.entries(options.computed))Object.defineProperty(page,key,{get:fn.bind(page)});
 return {page,options};
}
function response(body,{type='application/json',url='',text=''}={}){
 return {status:200,ok:true,redirected:false,url,headers:{get:name=>name==='content-type'?type:null},
  json:async()=>body,text:async()=>text};
}
(async()=>{
 const {page,options}=optionsFor();options.mounted.call(page);
 let statusReads=0,htmlReads=0;
 assert.equal(state.doc.querySelectorAll('[data-source-prepare-region]').length,2,'test document starts with exact two regions');
 global.fetch=async(url,request)=>{
  assert.equal(request.credentials,'same-origin');
  if(url==='/marketplaces/ozon/uploads/api/'+jobUid || url==='https://seller.test/marketplaces/ozon/uploads/api/'+jobUid){statusReads++;
   if(statusReads===1){const awaiting={...initial,status:'completed',source_prepare_complete:false,summary:{...initial.summary,processed:2,active:0,outcome:'success'}};
    return response({success:true,run:awaiting},{url:'https://seller.test'+url});}
   const terminal={...initial,status:'completed',source_prepare_complete:true,summary:{...initial.summary,processed:2,active:0,outcome:'partial'}};
   return response({success:true,run:terminal},{url:'https://seller.test'+url});
  }
  htmlReads++;assert.equal(url,'https://seller.test/marketplaces/ozon/uploads/'+jobUid);
  assert.equal(request.cache,'no-store');assert.equal(request.redirect,'error');
  assert.equal(request.headers.Accept,'text/html');
  return response(null,{type:'text/html; charset=utf-8',url,text:'terminal-page'});
 };

 await page.refresh();
 assert.equal(page.sourcePrepareAwaitingTerminal,true,'completed status without durable true must keep polling');
 assert.equal(page.active,true);assert.equal(htmlReads,0);

 const preservedLink=state.regions.product;state.doc.activeElement=state.regions.action;
 await page.refresh();
 assert.equal(statusReads,2);assert.equal(htmlReads,1);assert.equal(page.sourcePrepareSwapDeferred,true);
 assert.equal(page.sourcePrepareUpdated,false);assert.equal(state.doc.querySelectorAll('[data-source-prepare-complete="0"]').length,2);
 assert.equal(preservedLink.getAttribute('href'),'/my-products/beta#workspace-return='+'b'.repeat(32));

 state.doc.activeElement=new Element('button');state.doc.listeners.focusout();
 const deferred=Array.from(timers.entries()).find(([,timer])=>timer.delay===0);
 assert.ok(deferred,'focusout schedules the pending safe swap');timers.delete(deferred[0]);deferred[1].fn();
 const current=state.doc.querySelectorAll('[data-source-prepare-region]');
 assert.equal(current.length,2);assert.equal(current[0].getAttribute('data-source-prepare-complete'),'1');
 assert.equal(current[1].getAttribute('data-source-prepare-complete'),'1');
 assert.equal(current[0].querySelector('a[data-source-prepare-products]'),preservedLink,'preserve the session-bound workspace link node');
 assert.equal(current[1].querySelector('details.preparation-journey-path').open,true,'preserve expanded journey state');
 assert.equal(page.sourcePrepareUpdated,true);assert.equal(page.changed,false);
 assert.equal(htmlReads,1,'one terminal HTML GET');options.beforeUnmount.call(page);
 assert.equal(state.doc.listeners.focusout,undefined);

 const mismatch=makeDocument('0');global.document=mismatch.doc;let mismatchReads=0;
 const second=optionsFor();second.options.mounted.call(second.page);
 global.fetch=async(url)=>{mismatchReads++;const wrong={...initial,status:'completed',account_id:18,source_prepare_complete:true,
  summary:{...initial.summary,account_id:18,processed:2,active:0,outcome:'success'}};
  return response({success:true,run:wrong},{url:'https://seller.test'+url});};
 await second.page.refresh();
 assert.equal(mismatchReads,1,'scope mismatch stops the poll before HTML');
 assert.equal(second.page.changed,true);assert.match(second.page.error,/область подготовки/);
 assert.ok(mismatch.doc.querySelector('[data-source-prepare-refresh-fallback]'));
 assert.equal(timers.size,0,'mismatch does not schedule another poll');
 second.options.beforeUnmount.call(second.page);

 const missingBit=makeDocument('0');global.document=missingBit.doc;let missingBitReads=0;
 const third=optionsFor();third.options.mounted.call(third.page);
 global.fetch=async(url)=>{missingBitReads++;const unsupported={...initial,status:'completed',source_prepare_complete:undefined,
  summary:{...initial.summary,processed:2,active:0,outcome:'success'}};
  return response({success:true,run:unsupported},{url:'https://seller.test'+url});};
 await third.page.refresh();
 assert.equal(missingBitReads,1,'missing explicit completion bit stops automatic polling');
 assert.equal(third.page.changed,true);assert.match(third.page.error,/область подготовки/);
 assert.equal(missingBit.doc.querySelectorAll('[data-source-prepare-complete="0"]').length,2);
 assert.equal(missingBit.doc.querySelector('[data-source-prepare-refresh-fallback]')!==null,true);
 assert.equal(timers.size,0,'missing completion bit does not schedule another poll');
 third.options.beforeUnmount.call(third.page);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
