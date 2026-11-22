"""State contracts for the actual Vue controller; no implementation text matching."""
from pathlib import Path
import subprocess


def test_bulk_review_and_uncertain_save_contracts():
    source=Path(__file__).parents[1]/'static/ozon-bulk-repair-vue.js'
    script=r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const saved=new Map();
const window={ozonDraftEditor:{fieldComponent:{},photoComponent:{}},document:{activeElement:{focus(){}},getElementById(){return null}},
  location:{href:'https://app.test/repair',origin:'https://app.test',pathname:'/repair',search:''},history:{replaceState(){}},
  sessionStorage:{setItem(k,v){saved.set(k,v)},getItem(k){return saved.get(k)}}};
let transport=async()=>{throw Error('Unexpected fetch')},calls=[];
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{window,URL,URLSearchParams,TextEncoder,AbortController,
  setTimeout,clearTimeout,fetch:async(url,options)=>{calls.push({url,method:options.method||'GET',body:options.body?.toString(),headers:options.headers});return transport(url,options);}});
const core=window.ozonBulkRepair;
const field=(id,type='String',dictionary=false,value='',dictionary_value_id='')=>({external_id:id,name:'Поле '+id,value,dictionary_value_id,
  editable:true,dictionary,dictionary_fresh:true,data_type:type,is_collection:false,max_values:1});
const row=(id,attrs=[],productTypeId=1)=>({draft_id:id,draft_version:1,imported_product_id:id,offer_id:'offer-'+id,title:'Товар '+id,
  action:'ИСПРАВИТЬ',product_type_id:productTypeId,product_type_name:'Тип '+productTypeId,price_rub:'100',description:'Описание',
  package_width_mm:'',package_height_mm:'',package_depth_mm:'',package_weight_g:'',attributes:attrs,cleanup_candidates:[],
  schema_cleanup:false,status:'needs_input',validation_errors:[],type_change_impact:{attributes:0,complex_groups:0,removals:0}});
const editor=()=>({job_uid:'synthetic-run',account_id:1,groups:[{id:'g1',source_category:'Категория',rows:[
  row(1,[field('10','Boolean'),field('20','String',true)]),
  row(2,[field('10'),field('20','String',true,'Официальное','700001')]),
  row(3,[field('20','String',true)],2)]}],
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
  const exact={value:'Официальное',dictionary_value_id:'700002'};
  s.bulkKey='attribute:20';s.bulkMode='value';s.bulkValues=[exact];s.previewBulk();
  plan=s.bulkPlan;
  assert.equal(plan.items.map(item=>item.id).join(','),'1,2,3');
  assert.equal(plan.items[0].after[0].dictionary_value_id,'700002','Value mode keeps the exact ID for the same saved type');
  assert.equal(plan.items[1].after[0].dictionary_value_id,'700002','Same label with a different ID is an actual same-type change');
  assert.equal(plan.items[2].after[0].dictionary_value_id,undefined,'Mixed-type values remain label-only for target re-resolution');
  assert.equal(s.dictionaryIdentities(plan.items[0].after)[0],'ID 700002 в справочнике Ozon');
  s.rows[1].attributes[1].values[0].dictionary_value_id='';
  plan=core.planBulk([s.rows[1]],'attribute:20',[exact],s.rows[0].attributes[1],1);
  assert.equal(plan.items.length,1,'Same label without an ID must receive the explicit ID');
  s.rows[1].attributes[1].values[0].dictionary_value_id='700002';
  plan=core.planBulk([s.rows[1]],'attribute:20',[exact],s.rows[0].attributes[1],1);
  assert.equal(plan.items.length,0);assert.equal(plan.skipped[0].reason,'Значение уже совпадает','Exact label and ID is a no-op');
  const identityChange=core.changes(s.rows[1]).find(change=>change.key==='attribute:20');
  assert(identityChange.showDictionaryIdentities,'Same-label generic ID changes are visible in review');
  assert.equal(identityChange.beforeIdentities[0],'ID 700001 в справочнике Ozon');
  assert.equal(identityChange.afterIdentities[0],'ID 700002 в справочнике Ozon');
  assert.equal(s.shouldShowDictionaryId('20',[{id:'700001',value:'Хлопок'}]),false,'Unique generic labels keep their prior picker text');
  assert.equal(s.shouldShowDictionaryId('20',[{id:'700001',value:'Хлопок'},{id:'700002',value:'Хлопок'}]),true,'Duplicate generic labels expose their IDs');
  assert.equal(s.shouldShowDictionaryId('22232',[{id:'700002',value:'3304990000'}]),true,'TN VED always exposes its exact ID');

  s=state();s.rows.forEach(r=>r.selected=true);s.bulkKey='attribute:20';s.bulkMode='copy';s.bulkSource=2;
  s.previewBulk();plan=s.bulkPlan;
  assert.equal(plan.items.find(item=>item.id===1).after[0].dictionary_value_id,'700001','Copy mode keeps the source ID for same saved type');
  assert.equal(plan.items.find(item=>item.id===3).after[0].dictionary_value_id,undefined,'Copy mode re-resolves mixed types by label');
  s.rows[1].product_type_id=9;
  s.previewBulk();plan=s.bulkPlan;
  assert.equal(plan.items.find(item=>item.id===1).after[0].dictionary_value_id,undefined,'A staged source type cannot authorize its old ID');
  s.rows[0].attributes[0].values=[{value:'false'}];
  assert.equal(core.serialize([s.rows[0]],'csrf').get('row_1_attribute_10'),'false');
  s.rows[0].attributes[1].values=[exact];
  assert.equal(core.serialize([s.rows[0]],'csrf').get('row_1_attribute_value_id_20'),'700002','A singleton dictionary ID reaches the existing form field');
  s.rows[0].attributes[1].is_collection=true;s.rows[0].attributes[1].max_values=2;
  s.rows[0].attributes[1].values=[exact,{value:'Второе',dictionary_value_id:'700003'}];
  assert.equal(core.serialize([s.rows[0]],'csrf').get('row_1_attribute_value_id_20'),'','Multi-value ID transport remains unsupported');
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


def test_draft_selection_keeps_exact_selection_and_requires_local_confirmation():
    source=Path(__file__).parents[1]/'static/ozon-bulk-repair-vue.js'
    script=r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const saved=new Map(),scope='a'.repeat(64),active={focus(){}};
const window={ozonDraftEditor:{fieldComponent:{},photoComponent:{}},document:{activeElement:active,getElementById(){return null}},
 location:{href:'https://app.test/repair?account_id=1&draft_ids=1',origin:'https://app.test',pathname:'/repair',search:'?account_id=1&draft_ids=1'},
 history:{replaceState(){}},addEventListener(){},removeEventListener(){},sessionStorage:{setItem(k,v){saved.set(k,v)},getItem(k){return saved.get(k)},removeItem(k){saved.delete(k)}}};
let transport=async()=>{throw Error('Unexpected fetch')},calls=[];
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{window,URL,URLSearchParams,TextEncoder,AbortController,
 setTimeout,clearTimeout,fetch:async(url,options)=>{calls.push({url,method:options.method||'GET',body:options.body?.toString()});return transport(url,options);}});
const core=window.ozonBulkRepair;
const fixture=()=>({draft_id:1,draft_version:1,imported_product_id:7,offer_id:'offer-1',title:'Товар один',action:'ИСПРАВИТЬ',
 product_type_id:4,product_type_name:'Тип',price_rub:'100',description:'Описание',package_width_mm:'',package_height_mm:'',
 package_depth_mm:'',package_weight_g:'',attributes:[],cleanup_candidates:[],schema_cleanup:false,status:'needs_input',
 validation_errors:[],type_change_impact:{attributes:0,complex_groups:0,removals:0}});
const editor=()=>({mode:'draft_selection',scope_key:scope,job_uid:null,account_id:1,account_label:'Магазин один',
 groups:[{id:'g',source_category:'Категория',rows:[fixture()]}],review_draft_ids:[1],blocked_review_items:[],bulk_fields:[{key:'price_rub',label:'Цена продавца'}]});
const config={editor:editor(),enabled:true,csrf:'csrf',urls:{read:'/read?account_id=1&draft_ids=1',apply:'/apply?account_id=1&draft_ids=1',types:'/types?account_id=1&draft_ids=1',
 dictionaries:'/dict/',dictionary_query:'account_id=1&draft_ids=1',drafts:'/draft/',result:'/drafts?account_id=1',review:'/review?account_id=1&draft_ids=1',classic:'/classic'}};
function state(){const options=core.createOptions(config),value=options.data();for(const [key,fn]of Object.entries(options.methods))value[key]=fn.bind(value);
 for(const [key,fn]of Object.entries(options.computed))Object.defineProperty(value,key,{get:fn.bind(value)});
 value.$nextTick=fn=>fn();value.$refs={modal:{showModal(){},close(){}},error:{focus(){}}};return {options,value};}
const response=data=>({status:200,ok:true,redirected:false,json:async()=>({...data,csrf:'fresh-csrf'})});
(async()=>{
 let {options,value}=state();assert.deepEqual(value.rows.map(row=>row.selected),[true],'initial editor selection checks every reviewable requested row');
 assert.equal(core.validEditor(config.editor,config),true);
 const wrongScope=editor();wrongScope.scope_key='b'.repeat(64);assert.equal(core.validEditor(wrongScope,config),false,'readback requires the same stable scope digest');
 const wrongMode=editor();wrongMode.mode='source_prepare';assert.equal(core.validEditor(wrongMode,config),false,'readback requires selection mode');
 const mappingProbe={...value.rows[0],save_mapping:true};
 const payload=core.serialize([mappingProbe],'csrf',false,true);assert.equal(payload.get('confirm_local'),'1');
 assert.equal(payload.get('row_1_save_mapping'),'0','selected-draft saves cannot update shared category rules');
 assert.equal(core.serialize([mappingProbe],'csrf',false,false).get('row_1_save_mapping'),'1','legacy saves retain their existing mapping control');
 assert.equal(payload.get('row_1_action'),'ИСПРАВИТЬ');
 let receiptFocused=false;value.$refs.receipt={isConnected:true,focus(){receiptFocused=true;},scrollIntoView(){}};
 value.rows[0].price_rub='120';value.reviewSave();assert.equal(value.review[0].action,'ИСПРАВИТЬ');
 const after=editor();after.groups[0].rows[0].draft_version=2;after.groups[0].rows[0].price_rub='120';
 transport=async url=>url==='/read?account_id=1&draft_ids=1'
   ? response({success:true,editor:after})
   : response({success:true,mode:'draft_selection',scope_key:scope,report:{mode:'draft_selection',scope_key:scope,total:1,updated:1,
      ready_to_retry:0,needs_input:1,failed:0,rows:[{draft_id:1,status:'needs_input',version:2}]}});
 await value.save();assert.equal(value.uncertain,false,'matching save and readback should clear uncertainty: '+value.error);assert.equal(value.report.updated,1);
 assert.equal(receiptFocused,true,'verified local save returns focus to the updated receipt');
 const sent=new URLSearchParams(calls.find(call=>call.method==='POST').body);
 assert.equal(sent.get('confirm_local'),'1');assert.equal(sent.get('row_1_action'),'ИСПРАВИТЬ');
 let {value:bad}=state();bad.rows[0].price_rub='125';bad.reviewSave();
 transport=async()=>response({success:true,mode:'draft_selection',scope_key:'b'.repeat(64),report:{mode:'draft_selection',scope_key:'b'.repeat(64),total:1,rows:[{draft_id:1,status:'needs_input'}]}});
 await bad.save();assert.equal(bad.uncertain,true);assert.equal(bad.rows[0].price_rub,'125');
 assert(bad.error.includes('не совпал'),'scope mismatch retains input and blocks confirmation');
 saved.set('ozon-repair:1:draft-selection:'+encodeURIComponent(scope),JSON.stringify({selected:[],uncertain:false}));
 const restored=state();restored.options.mounted.call(restored.value);
 assert.deepEqual(restored.value.rows.map(row=>row.selected),[false],'an explicit persisted deselection overrides selection defaults');
 console.log('draft-selection initial rows, exact scope, local confirmation and uncertain save: passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    template=(Path(__file__).parents[1]/'templates/ozon_bulk_repair.html').read_text()
    assert 'data-draft-selection-editor' in template
    assert 'data-draft-selection-save' in template
    assert 'data-draft-selection-review' in template
    assert "'Обработано черновиков: '" in template
    assert 'Изменено: {{ report.updated }}' in template
    assert template.count('v-if="!draftSelection" class="orb-check"') == 2
