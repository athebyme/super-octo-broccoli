"""State contracts for the actual Vue controller; no implementation text matching."""
from pathlib import Path
import subprocess


def test_bulk_review_and_uncertain_save_contracts():
    source=Path(__file__).parents[1]/'static/ozon-bulk-repair-vue.js'
    script=r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const saved=new Map();
const window={ozonDraftEditor:{fieldComponent:{},photoComponent:{}},document:{activeElement:{focus(){}},getElementById(){return null}},
  location:{href:'https://app.test/repair',pathname:'/repair',search:''},history:{replaceState(){}},
  sessionStorage:{setItem(k,v){saved.set(k,v)},getItem(k){return saved.get(k)}}};
let transport=async()=>{throw Error('Unexpected fetch')},calls=[];
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{window,URL,URLSearchParams,TextEncoder,AbortController,
  setTimeout,clearTimeout,fetch:async(url,options)=>{calls.push({url,method:options.method||'GET',body:options.body?.toString(),headers:options.headers});return transport(url,options);}});
const core=window.ozonBulkRepair;
const field=(id,type='String',dictionary=false)=>({external_id:id,name:'Поле '+id,value:'',dictionary_value_id:'',
  editable:true,dictionary,dictionary_fresh:true,data_type:type,is_collection:false,max_values:1});
const row=(id,attrs=[])=>({draft_id:id,draft_version:1,imported_product_id:id,offer_id:'offer-'+id,title:'Товар '+id,
  action:'ИСПРАВИТЬ',product_type_id:1,product_type_name:'Тип 1',price_rub:'100',description:'Описание',
  package_width_mm:'',package_height_mm:'',package_depth_mm:'',package_weight_g:'',attributes:attrs,cleanup_candidates:[],
  schema_cleanup:false,status:'needs_input',validation_errors:[],type_change_impact:{attributes:0,complex_groups:0,removals:0}});
const editor=()=>({job_uid:'synthetic-run',account_id:1,groups:[{id:'g1',source_category:'Категория',rows:[
  row(1,[field('10','Boolean'),field('20','String',true)]),row(2,[field('10'),field('20','String',true)]),row(3,[])]}],
  bulk_fields:[{key:'price_rub',label:'Цена продавца'}]});
const config={editor:editor(),enabled:true,csrf:'synthetic',urls:{read:'/read',apply:'/apply',types:'/types',dictionaries:'/dict/'}};
function state(doc=editor()){
  const options=core.createOptions({...config,editor:doc}),s=options.data();
  for(const [k,v] of Object.entries(options.methods))s[k]=v.bind(s);
  for(const [k,v] of Object.entries(options.computed))Object.defineProperty(s,k,{get:()=>v.call(s)});
  s.$nextTick=fn=>fn();s.$refs={modal:{showModal(){},close(){}},error:{focus(){}}};return s;
}
const json=v=>JSON.parse(JSON.stringify(v));
const ok=data=>({status:200,ok:true,redirected:false,json:async()=>({...data,csrf:'renewed-synthetic-csrf'})});
(async()=>{
  let s=state();s.rows.forEach(r=>r.selected=true);
  let plan=core.planBulk(s.rows,'attribute:10',[{value:'false'}],s.rows[0].attributes[0]);
  assert.equal(plan.items.length,1);assert.equal(plan.skipped.length,2);assert.equal(plan.items[0].after[0].value,'false');
  plan=core.planBulk(s.rows,'attribute:20',[{value:'Официальное',dictionary_value_id:'999'}],s.rows[0].attributes[1]);
  assert.equal(plan.items.length,2);assert.equal(plan.items[0].after[0].dictionary_value_id,undefined);
  s.rows[0].attributes[0].values=[{value:'false'}];
  assert.equal(core.serialize([s.rows[0]],'csrf').get('row_1_attribute_10'),'false');
  s.rows[0].product_type_id=2;
  assert.equal(core.serialize([s.rows[0]],'csrf').get('row_1_attribute_10'),'');
  assert.equal(s.rows[0].attributes[0].values[0].value,'false','Changing type keeps staged input until explicit save');
  s=state();s.rows[0].selected=true;s.rows[0].price_rub='120';s.reviewSave();
  assert.equal(s.dialog,'review');assert.equal(s.review.length,1);assert.equal(calls.length,0,'Review has no I/O');
  transport=async()=>{throw Error('Lost response')};await s.save();
  assert.equal(calls.filter(c=>c.method==='POST').length,1);assert.equal(s.uncertain,true);assert.equal(s.rows[0].price_rub,'120');
  assert.equal(JSON.parse([...saved.values()].at(-1)).uncertain,true);
  await s.save();assert.equal(calls.filter(c=>c.method==='POST').length,1,'Unknown save is not repeated');
  assert(![...saved.values()].some(v=>v.includes('120') || v.includes('Описание')),'No draft text in browser storage');
  const fresh=editor();fresh.groups[0].rows[0].draft_version=2;fresh.groups[0].rows[0].price_rub='110';
  transport=async()=>ok({success:true,editor:fresh});await s.reload({manual:true});
  assert.equal(s.uncertain,false);assert.equal(s.rows[0].price_rub,'120');assert.equal(s.rows[0].server.price_rub,'110');
  s.conflictId=1;s.resolveConflict(true);assert.equal(s.rows[0].draft_version,2);assert.equal(s.rows[0].price_rub,'120');
  assert.equal(s.rows[0].base.price_rub,'110','Explicit rebase uses current version');
  s.reviewSave();transport=async()=>{throw Error('lost')};await s.save();
  assert.equal(new URLSearchParams(calls.at(-1).body).get('csrf_token'),'renewed-synthetic-csrf');
  assert.equal(calls.at(-1).headers['X-CSRFToken'],'renewed-synthetic-csrf');
  s=state();s.rows[0].price_rub='120';s.rows[1].price_rub='bad';
  const after=editor();after.groups[0].rows[0].price_rub='120';after.groups[0].rows[0].draft_version=2;
  transport=async()=>ok({success:true,editor:after});
  await s.reload({report:{rows:[{draft_id:1,status:'needs_input',version:2},{draft_id:2,status:'failed',message:'Цена некорректна'}]}});
  assert.equal(s.rows[0].draft_version,2);assert.equal(core.changes(s.rows[0]).length,0);
  assert.equal(s.rows[1].price_rub,'bad');assert.equal(s.rows[1].error,'Цена некорректна');
  const foreign=editor();foreign.account_id=2;transport=async()=>ok({success:true,editor:foreign});
  await s.reload({manual:true});assert.equal(s.rows[1].price_rub,'bad');assert(s.error.includes('магазин'));
  s.uncertain=true;transport=async()=>({status:200,ok:true,json:async()=>({success:true,editor:after})});
  await s.reload({manual:true});assert.equal(s.uncertain,true);assert.equal(s.rows[1].price_rub,'bad');assert(s.error.includes('защиту формы'));
  s=state();s.rows[0].selected=true;s.rows[0].price_rub='125';s.reviewSave();
  transport=async()=>({status:401,ok:false,redirected:false,json:async()=>({})});await s.save();
  assert.equal(s.sessionExpired,true);assert.equal(s.rows[0].price_rub,'125');assert.equal(s.locked,true);
  // A superseded failed reference search must not replace newer results.
  s=state();s.picker.kind='type';let rejectOld;
  transport=async(url)=>url.includes('old') ? new Promise((_,reject)=>{rejectOld=reject}) : ok({success:true,items:[{id:5,name:'new'}]});
  s.picker.query='old';s.search();await new Promise(r=>setTimeout(r,280));
  s.picker.query='new';s.search();await new Promise(r=>setTimeout(r,280));rejectOld(Error('stale failure'));
  await new Promise(r=>setTimeout(r,5));assert.equal(s.picker.items[0].name,'new');assert.equal(s.picker.error,'');
  s.cancelSearch();
  console.log('bulk review, partial result, unknown outcome, scope, session, search race: passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
