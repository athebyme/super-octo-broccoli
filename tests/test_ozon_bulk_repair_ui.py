"""State contracts for the actual Vue controller; no implementation text matching."""
from pathlib import Path
import subprocess

from flask import Flask


def test_bulk_review_and_uncertain_save_contracts():
    source=Path(__file__).parents[1]/'static/ozon-bulk-repair-vue.js'
    script=r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const saved=new Map();
const window={ozonDraftEditor:{fieldComponent:{},photoComponent:{}},document:{activeElement:{focus(){}},getElementById(){return null}},
  location:{href:'https://app.test/repair',origin:'https://app.test',pathname:'/repair',search:''},history:{replaceState(){}},
  sessionStorage:{setItem(k,v){saved.set(k,v)},getItem(k){return saved.get(k)}}};
let transport=async()=>{throw Error('Unexpected fetch')},calls=[];
const context={window,URL,URLSearchParams,TextEncoder,TextDecoder,AbortController,
  setTimeout,clearTimeout,fetch:async(url,options)=>{calls.push({url,method:options.method||'GET',body:options.body?.toString(),headers:options.headers});return transport(url,options);}};
vm.runInNewContext(fs.readFileSync(process.argv[1].replace('ozon-bulk-repair-vue.js','ozon-workspace-return.js'),'utf8'),context);
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
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
 history:{replaceState(){}},btoa:value=>Buffer.from(value,'binary').toString('base64'),atob:value=>Buffer.from(value,'base64').toString('binary'),
 addEventListener(){},removeEventListener(){},sessionStorage:{setItem(k,v){saved.set(k,v)},getItem(k){return saved.get(k)},removeItem(k){saved.delete(k)}}};
let transport=async()=>{throw Error('Unexpected fetch')},calls=[];
const context={window,URL,URLSearchParams,TextEncoder,TextDecoder,AbortController,
 setTimeout,clearTimeout,fetch:async(url,options)=>{calls.push({url,method:options.method||'GET',body:options.body?.toString()});return transport(url,options);}};
vm.runInNewContext(fs.readFileSync(process.argv[1].replace('ozon-bulk-repair-vue.js','ozon-workspace-return.js'),'utf8'),context);
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const core=window.ozonBulkRepair;
const fixture=()=>({draft_id:1,draft_version:1,imported_product_id:7,offer_id:'offer-1',title:'Товар один',action:'ИСПРАВИТЬ',source_refresh_available:false,
 product_type_id:4,product_type_name:'Тип',price_rub:'100',description:'Описание',package_width_mm:'',package_height_mm:'',
 package_depth_mm:'',package_weight_g:'',attributes:[],cleanup_candidates:[],schema_cleanup:false,status:'needs_input',
 validation_errors:[],type_change_impact:{attributes:0,complex_groups:0,removals:0}});
const editor=()=>({mode:'draft_selection',scope_key:scope,job_uid:null,account_id:1,account_label:'Магазин один',
 groups:[{id:'g',source_category:'Категория',rows:[fixture()]}],review_draft_ids:[1],blocked_review_items:[],bulk_fields:[{key:'price_rub',label:'Цена продавца'}]});
const config={editor:editor(),enabled:true,csrf:'csrf',urls:{read:'/read?account_id=1&draft_ids=1',apply:'/apply?account_id=1&draft_ids=1',types:'/types?account_id=1&draft_ids=1',
 dictionaries:'/dict/',dictionary_query:'account_id=1&draft_ids=1',drafts:'/draft/',result:'/drafts?account_id=1',review:'/review?account_id=1&draft_ids=1',classic:'/classic'}};
function state(activeConfig=config){const options=core.createOptions(activeConfig),value=options.data();for(const [key,fn]of Object.entries(options.methods))value[key]=fn.bind(value);
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
 assert.equal(core.encodeCompactDraftIds([1]),'AAAAAAAAAAE','compact IDs use ordered eight-byte big-endian values');
 const compactConfig={...config,urls:{...config.urls,review:'/review?account_id=1&workspace_selection=AAAAAAAAAAE'}};
 const scoped=state(compactConfig);transport=async()=>response({success:true,editor:editor()});
 assert.equal(await scoped.value.reload(),true);
 const scopedReview=new URL(scoped.value.urls.review,'https://app.test');
 assert.equal(scopedReview.searchParams.get('workspace_selection'),'AAAAAAAAAAE');
 assert.equal(scopedReview.searchParams.has('draft_ids'),false,'compact review scope stays exclusive after readback');
 const nonce='0123456789abcdef0123456789abcdef';
 const nested='/my-products/beta?import_batch='+'A'.repeat(8188)+'.B.C#workspace-return='+nonce;
 assert.equal(core.safeWorkspaceReturn(nested),nested,'the maximum supported import-batch context survives exact validation');
 assert.equal(core.safeWorkspaceReturn('https://outside.test/my-products/beta'),null,'external workspace returns fail closed');
 const capsule=Buffer.from(nested,'utf8').toString('base64url');
 window.location.href='https://app.test/marketplaces/ozon/uploads/selection/repair?account_id=1&workspace_selection=AAAAAAAAAAE&workspace_return=1#workspace-return='+capsule;
 assert.equal(core.workspaceReturnFromFragment(),nested,'the nested return rides the browser fragment, outside the request line');
 window.location.href='https://app.test/repair?workspace_return=1#workspace-return='+'A'.repeat(11265);
 assert.equal(core.workspaceReturnFromFragment(),null,'oversized fragments are rejected before decode');
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
    assert 'v-text="urls.catalog_return_label || \'К каталогу Ozon\'"' in template
    assert "filename='ozon-workspace-return.js', v='20261122-ozon-workspace-review-return-r57'" in template
    assert "filename='ozon-bulk-repair-vue.css', v='20261123-ozon-source-review-r1'" in template
    assert "filename='ozon-bulk-repair-vue.js', v='20261123-ozon-source-refresh-r1'" in template
    assert 'data-source-facts-refresh' in template
    assert 'data-source-facts-refresh-dialog' in template
    assert 'data-source-facts-refresh-count' in template
    assert 'data-source-facts-refresh-confirm' in template
    assert 'data-source-facts-refresh-result' in template


def test_native_supplier_comparisons_use_exact_sets_and_bound_local_decisions():
    source=Path(__file__).parents[1]/'static/ozon-bulk-repair-vue.js'
    script=r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {webcrypto,createHash}=require('node:crypto');
const scope='a'.repeat(64),saved=new Map(),active={focus(){}};
const colors=[{value:'Розовый',dictionary_value_id:'1001'},{value:'Белый',dictionary_value_id:'1002'}];
const comparison=(id,sourceField,status,literal_labels,resolved_values,evidence_receipt,reason=null,reason_label=null)=>({
  attribute_id:id,complex_id:'0',source_field:sourceField,status,reason,reason_label,literal_labels,resolved_values,evidence_receipt});
const field=(id,name,value,ids,required=false,max_values=2)=>({external_id:id,name,value,dictionary_value_ids:ids,dictionary_value_id:'',
  editable:true,source_managed:true,required,dictionary:true,dictionary_fresh:true,data_type:'String',is_collection:true,max_values});
const makeEditor=()=>({mode:'draft_selection',scope_key:scope,job_uid:null,account_id:1,account_label:'Магазин один',
  groups:[{id:'g',source_category:'Категория',rows:[{draft_id:196,draft_version:3,imported_product_id:801,offer_id:'offer-196',title:'Пальто',
    action:'ИСПРАВИТЬ',source_refresh_available:false,product_type_id:4,product_type_name:'Одежда',price_rub:'100',description:'Описание',package_width_mm:'',package_height_mm:'',
    package_depth_mm:'',package_weight_g:'',attributes:[field('10096','Цвет товара','Синий\nЗелёный',['5001','5002']),
      field('4389','Страна-изготовитель','Россия',['643'],false)],cleanup_candidates:[],schema_cleanup:false,status:'needs_input',validation_errors:[],
    type_change_impact:{attributes:0,complex_groups:0,removals:0},source_comparisons:{
      '10096':comparison('10096','colors','confirmed',['Розовый','Белый'],colors,'signed-colors'),
      '4389':comparison('4389','country','unknown',[],[],null,'source_field_not_observed','Поставщик явно не передавал это поле')}}]}],
  review_draft_ids:[196],blocked_review_items:[],bulk_fields:[{key:'price_rub',label:'Цена продавца'}]});
const doc=makeEditor(),config={editor:doc,enabled:true,csrf:'csrf',urls:{read:'/read',apply:'/apply',types:'/types',dictionaries:'/dict/'}};
const window={crypto:webcrypto,ozonDraftEditor:{fieldComponent:{},photoComponent:{}},document:{activeElement:active,getElementById(){return null}},
  location:{href:'https://app.test/repair?account_id=1&draft_ids=196',origin:'https://app.test',pathname:'/repair',search:'?account_id=1&draft_ids=196'},
  history:{replaceState(){}},addEventListener(){},removeEventListener(){},sessionStorage:{setItem(k,v){saved.set(k,v)},getItem(k){return saved.get(k)}}};
const context={window,URL,URLSearchParams,TextEncoder,TextDecoder,AbortController,setTimeout,clearTimeout,fetch:async()=>{throw Error('Unexpected fetch')}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const core=window.ozonBulkRepair,options=core.createOptions(config),state=options.data();
for(const [key,fn] of Object.entries(options.methods))state[key]=fn.bind(state);
for(const [key,fn] of Object.entries(options.computed))Object.defineProperty(state,key,{get:()=>fn.call(state)});
state.$nextTick=fn=>fn();state.$refs={modal:{showModal(){},close(){}},error:{focus(){}}};
;(async()=>{
assert.equal(core.validEditor(doc,config),true,'native DTO preserves exact collection source comparisons');
assert.equal(core.definition(state.rows[0].attributes[0]).required,false,'optional source field stays optional in its editor');
assert.equal(state.sourceComparisonsFor(196).length,0,'unchanged fields do not add comparison rows');
const row=state.rows[0],color=row.attributes.find(item=>item.external_id==='10096'),country=row.attributes.find(item=>item.external_id==='4389');
state.review=[{id:196,title:row.title,action:'ИСПРАВИТЬ',changes:[]}];
state.changeField(row,color,[{value:'Белый',dictionary_value_id:'1002'},{value:'Розовый',dictionary_value_id:'1001'}]);
state.changeField(row,country,[{value:'Китай',dictionary_value_id:'156'}]);
let comparisons=state.sourceComparisonsFor(196);
assert.equal(comparisons.map(item=>item.attribute_id).join(','),'10096,4389','only the two changed target fields render');
assert.equal(state.sourceDecisionRequired(196,comparisons[0]),false,'an exact resolved collection set matches regardless of order');
assert.equal(state.sourceEvidenceStatusLabel(comparisons[1]),'Нет подтверждённых данных','missing source data is explicit');
assert.equal(state.sourceDecisionRequired(196,comparisons[1]),false,'unknown evidence adds no acknowledgement');
assert.equal(state.sourceCurrentLabel(196,comparisons[0]),'Синий, Зелёный');
assert.equal(state.sourceProposedLabel(196,comparisons[0]),'Белый, Розовый');
assert.equal(state.sourceHasDictionaryIds(196,comparisons[0]),true,'all exact IDs remain available in the details toggle');
assert.equal(state.sourceDecisionsResolved,true,'matching and unknown values need no extra decision');
assert.equal(state.genericReviewItems.length,0,'rows with only source comparisons do not repeat their product headings below');
row.description='Новое описание';state.review[0].changes=state.changes(row);
assert.equal(state.genericReviewItems.length,1,'remaining non-source edits stay in the generic review');
assert.equal(state.genericReviewHasHeading,true,'remaining edits receive a short contextual heading');
let json=await state.sourceReviewJSON([row]);
let entries=JSON.parse(json[196]);
assert.equal(entries['10096'].ack,false,'a changed source-equal proposal carries evidence without an acknowledgement');
assert.equal(entries['4389'],undefined,'unknown evidence has no signed decision entry');
const matchedCanonical='[{"dictionary_value_id":"","value":"Белый"},{"dictionary_value_id":"","value":"Розовый"}]';
assert.equal(entries['10096'].proposal_sha256,createHash('sha256').update(matchedCanonical,'utf8').digest('hex'));
let body=core.serialize([row],'csrf',false,true,json);
assert.equal(body.get('row_196_attribute_10096'),'Белый\nРозовый');
assert.equal(body.get('row_196_attribute_value_id_10096'),'','multi-value transport has no per-cell IDs');
assert.equal(JSON.parse(body.get('row_196_source_review_json'))['10096'].ack,false);

const conflictDoc=makeEditor(),conflictConfig={...config,editor:conflictDoc},conflictOptions=core.createOptions(conflictConfig),conflict=conflictOptions.data();
for(const [key,fn] of Object.entries(conflictOptions.methods))conflict[key]=fn.bind(conflict);
for(const [key,fn] of Object.entries(conflictOptions.computed))Object.defineProperty(conflict,key,{get:()=>fn.call(conflict)});
conflict.$nextTick=fn=>fn();conflict.$refs={modal:{showModal(){},close(){}},error:{focus(){}}};
const conflictRow=conflict.rows[0],conflictField=conflictRow.attributes[0],conflictComparison=conflict.sourceComparisonsFor(196)[0] || conflictRow.source_comparisons['10096'];
conflict.review=[{id:196,title:conflictRow.title,action:'ИСПРАВИТЬ',changes:[]}];
conflict.changeField(conflictRow,conflictField,[{value:'Жёлтый',dictionary_value_id:'3001'},{value:'Оранжевый',dictionary_value_id:'3002'}]);
assert.equal(conflict.sourceDecisionRequired(196,conflictComparison),true,'a changed value set differing from confirmed source needs an individual decision');
conflict.review=[{id:196,title:conflictRow.title,action:'ИСПРАВИТЬ',changes:conflict.changes(conflictRow)}];
assert.equal(conflict.genericReviewItems.length,0,'source-only conflict detail is not duplicated below');
assert.equal(conflict.sourceDecisionsResolved,false);
conflict.setSourceDecision(196,conflictComparison,'acknowledge');
assert.equal(conflict.sourceDecisionState(196,conflictComparison),'acknowledged');
assert.equal(conflict.sourceDecisionsResolved,true);
json=await conflict.sourceReviewJSON([conflictRow]);entries=JSON.parse(json[196]);
assert.equal(entries['10096'].ack,true);
const conflictCanonical='[{"dictionary_value_id":"","value":"Жёлтый"},{"dictionary_value_id":"","value":"Оранжевый"}]';
assert.equal(entries['10096'].proposal_sha256,createHash('sha256').update(conflictCanonical,'utf8').digest('hex'));
conflict.changeField(conflictRow,conflictField,[{value:'Чёрный',dictionary_value_id:'4001'},{value:'Белый',dictionary_value_id:'1002'}]);
assert.equal(conflict.sourceDecisionState(196,conflictComparison),'unresolved','editing the proposal clears its old acknowledgement');

const excludedDoc=makeEditor(),excludedOptions=core.createOptions({...config,editor:excludedDoc}),excluded=excludedOptions.data();
for(const [key,fn] of Object.entries(excludedOptions.methods))excluded[key]=fn.bind(excluded);
for(const [key,fn] of Object.entries(excludedOptions.computed))Object.defineProperty(excluded,key,{get:()=>fn.call(excluded)});
excluded.$nextTick=fn=>fn();excluded.$refs={modal:{showModal(){},close(){}},error:{focus(){}}};
const excludedRow=excluded.rows[0],excludedField=excludedRow.attributes[0],excludedComparison=excludedRow.source_comparisons['10096'];
excluded.changeField(excludedRow,excludedField,[{value:'Жёлтый',dictionary_value_id:'3001'},{value:'Оранжевый',dictionary_value_id:'3002'}]);
excludedRow.description='Новое описание';excluded.review=[{id:196,title:excludedRow.title,action:'ИСПРАВИТЬ',changes:excluded.changes(excludedRow)}];
excluded.setSourceDecision(196,excludedComparison,'exclude');
assert.equal(JSON.stringify(excludedField.values),JSON.stringify([{value:'Синий',dictionary_value_id:'5001'},{value:'Зелёный',dictionary_value_id:'5002'}]));
assert.equal(excluded.sourceDecisionState(196,excludedComparison),'excluded');
assert.equal(excluded.genericReviewItems.length,1,'other row edits remain visible when one source field is excluded');
assert.equal(excluded.genericReviewHasHeading,true);
json=await excluded.sourceReviewJSON([excludedRow]);body=core.serialize([excludedRow],'csrf',false,true,json);
assert.equal(body.has('row_196_attribute_10096'),false,'excluded source field is omitted from POST');
assert.equal(body.has('row_196_attribute_value_id_10096'),false,'excluded source identity is omitted from POST');
assert.equal(body.has('row_196_source_review_json'),false,'exclusion does not send an acknowledgement');
assert.equal(body.get('row_196_description'),'Новое описание','other local edits remain in the same save');
console.log('supplier comparison scope, exact collections, evidence decisions, hashes and exclusions: passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    template=(Path(__file__).parents[1]/'templates/ozon_bulk_repair.html').read_text()
    for hook in ('data-supplier-source-review','data-supplier-source-row','data-supplier-current',
                 'data-supplier-source','data-supplier-proposed','data-supplier-conflict-ack',
                 'data-supplier-source-exclude','data-supplier-source-save'):
        assert hook in template


def test_native_source_refresh_uses_exact_stale_rows_and_preserves_pending_edits():
    source=Path(__file__).parents[1]/'static/ozon-bulk-repair-vue.js'
    script=r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const scope='c'.repeat(64),saved=new Map(),active={focus(){}};
const window={ozonDraftEditor:{fieldComponent:{},photoComponent:{}},document:{activeElement:active,getElementById(){return null}},
 location:{href:'https://app.test/repair?account_id=1&draft_ids=1,2,3',origin:'https://app.test',pathname:'/repair',search:'?account_id=1&draft_ids=1,2,3'},
 history:{replaceState(){}},addEventListener(){},removeEventListener(){},sessionStorage:{setItem(k,v){saved.set(k,v)},getItem(k){return saved.get(k)}}};
let transport=async()=>{throw Error('Unexpected fetch')},calls=[];
const context={window,URL,URLSearchParams,TextEncoder,TextDecoder,AbortController,setTimeout,clearTimeout,
 fetch:async(url,options)=>{calls.push({url,method:options.method||'GET',body:options.body?.toString(),headers:options.headers});return transport(url,options);}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const core=window.ozonBulkRepair;
const row=(id,stale)=>({draft_id:id,draft_version:1,imported_product_id:100+id,offer_id:'offer-'+id,title:'Товар '+id,
 action:'ИСПРАВИТЬ',source_refresh_available:stale,product_type_id:4,product_type_name:'Тип',price_rub:'100',description:'Описание '+id,
 package_width_mm:'',package_height_mm:'',package_depth_mm:'',package_weight_g:'',attributes:[],cleanup_candidates:[],schema_cleanup:false,
 status:'needs_input',validation_errors:[],type_change_impact:{attributes:0,complex_groups:0,removals:0}});
const editor=(versions=[1,1,1],fresh=[true,true,false])=>({mode:'draft_selection',scope_key:scope,job_uid:null,account_id:1,account_label:'Магазин один',
 groups:[{id:'g',source_category:'Категория',rows:[row(1,fresh[0]),row(2,fresh[1]),row(3,fresh[2])].map((value,index)=>({...value,draft_version:versions[index]}))}],
 review_draft_ids:[1,2,3],blocked_review_items:[],bulk_fields:[{key:'price_rub',label:'Цена продавца'}]});
const config={editor:editor(),enabled:true,csrf:'csrf',urls:{read:'/read?account_id=1&draft_ids=1,2,3',apply:'/apply?account_id=1&draft_ids=1,2,3',types:'/types',dictionaries:'/dict/'}};
const response=data=>({status:200,ok:true,redirected:false,json:async()=>({...data,csrf:'fresh-csrf'})});
const options=core.createOptions(config),state=options.data();
for(const [key,fn]of Object.entries(options.methods))state[key]=fn.bind(state);
for(const [key,fn]of Object.entries(options.computed))Object.defineProperty(state,key,{get:fn.bind(state)});
state.$nextTick=fn=>fn();let focused=false;
state.$refs={modal:{showModal(){},close(){}},sourceRefreshReceipt:{isConnected:true,focus(){focused=true},scrollIntoView(){}}};
(async()=>{
 assert.equal(core.validEditor(config.editor,config),true);
 const malformed=editor();delete malformed.groups[0].rows[0].source_refresh_available;
 assert.equal(core.validEditor(malformed,config),false,'source refresh eligibility must be an exact server boolean');
 assert.deepEqual(state.sourceRefreshTargets.map(item=>item.draft_id),[1,2],'fresh selected rows do not enter the refresh target');
 state.rows[0].description='Unsaved seller edit';
 assert.equal(state.openSourceRefresh(),true);
 assert.equal(state.dialog,'source_refresh');
 assert.deepEqual(state.sourceRefreshReview.map(item=>item.title),['Товар 1','Товар 2']);
 const after=editor([2,2,1],[false,false,false]);
 after.groups[0].rows[0].description='Описание 1';
 transport=async(_url,options)=>{
   if(options.method==='POST')return response({success:true,mode:'draft_selection',scope_key:scope,report:{
         mode:'draft_selection',scope_key:scope,source_refresh:true,total:2,updated:2,ready_to_retry:1,needs_input:1,excluded:0,failed:0,
     rows:[{draft_id:1,status:'ready_to_retry',version:2},{draft_id:2,status:'needs_input',version:2}]}});
   return response({success:true,editor:after});
 };
 await state.confirmSourceRefresh();
 assert.equal(calls.filter(call=>call.method==='POST').length,1);
 assert.equal(calls.filter(call=>call.method==='GET').length,1,'refresh is followed by one exact editor readback');
 const sent=new URLSearchParams(calls.find(call=>call.method==='POST').body);
 assert.equal(sent.get('csrf_token'),'csrf');assert.equal(sent.get('refresh_source'),'1');assert.equal(sent.get('confirm_local'),'1');
 assert.deepEqual(sent.getAll('selected_draft_id'),['1','2']);
 for(const id of [1,2]){
   assert.equal(sent.get(`row_${id}_draft_version`),'1');
   assert.equal(sent.get(`row_${id}_imported_product_id`),String(100+id));
 }
 assert.equal(sent.getAll('selected_draft_id').includes('3'),false);
 assert.equal(sent.has('row_1_description'),false,'pending form edits are not submitted as part of facts refresh');
 assert.equal(state.uncertain,false,'matching receipt plus current readback resolves the outcome');
 assert.equal(state.rows[0].description,'Unsaved seller edit','readback retains pending local form values');
 assert.equal(state.rows[0].server.draft_version,2,'refreshed server version is shown through the ordinary conflict path');
 assert.equal(state.rows[0].server.description,'Описание 1');
 assert.equal(state.rows[2].source_refresh_available,false,'unselected fresh row is read back unchanged');
 assert.equal(state.sourceRefreshResult.updated,2);assert.equal(focused,true,'verified refresh focuses its result receipt');
 assert.equal(calls.filter(call=>call.method==='POST').length,1,'readback does not repeat the local action');

 const jobEditor=editor();jobEditor.mode='source_prepare';jobEditor.job_uid='prepare-run';delete jobEditor.scope_key;
 const jobConfig={...config,editor:jobEditor,urls:{...config.urls,read:'/job-read',apply:'/job-apply'}};
 assert.equal(core.validEditor(jobEditor,jobConfig),true,'source_prepare rows carry the same explicit boolean eligibility');
 const jobOptions=core.createOptions(jobConfig),jobState=jobOptions.data();
 for(const [key,fn]of Object.entries(jobOptions.methods))jobState[key]=fn.bind(jobState);
 for(const [key,fn]of Object.entries(jobOptions.computed))Object.defineProperty(jobState,key,{get:fn.bind(jobState)});
 jobState.$nextTick=fn=>fn();jobState.$refs={modal:{showModal(){},close(){}}};
 jobState.rows[0].selected=true;
 let jobCalls=[];
 transport=async(_url,options)=>{jobCalls.push(options.method||'GET');return response({success:true,report:{
   mode:'draft_selection',source_refresh:true,total:1,updated:1,ready_to_retry:1,needs_input:0,excluded:0,failed:0,
   rows:[{draft_id:1,status:'ready_to_retry',version:2}]}});};
 assert.equal(jobState.openSourceRefresh(),true);
 await jobState.confirmSourceRefresh();
 assert.equal(jobState.uncertain,true,'wrong source_prepare report mode is an unconfirmed outcome');
 assert.deepEqual(jobCalls,['POST'],'mismatched mode blocks success readback and automatic repeat');
 console.log('native source refresh exact stale subset, local confirmation, readback and edit preservation: passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_ozon_bulk_repair_template_compiles_with_flask_loader():
    template_dir=Path(__file__).parents[1]/'templates'
    app=Flask(__name__,template_folder=str(template_dir))
    compiled=app.jinja_env.get_template('ozon_bulk_repair.html')
    assert compiled is not None


def test_mixed_bulk_field_picker_filters_selected_schema_and_unavailable_rows():
    source=Path(__file__).parents[1]/'static/ozon-bulk-repair-vue.js'
    script=r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const saved=new Map(),active={focus(){}};
const window={ozonDraftEditor:{fieldComponent:{},photoComponent:{}},document:{activeElement:active,getElementById(){return null}},
 location:{href:'https://app.test/repair',origin:'https://app.test',pathname:'/repair',search:''},history:{replaceState(){}},
 addEventListener(){},removeEventListener(){},sessionStorage:{setItem(k,v){saved.set(k,v)},getItem(k){return saved.get(k)}}};
let transport=async()=>{throw Error('Unexpected fetch')};
const context={window,URL,URLSearchParams,TextEncoder,TextDecoder,AbortController,
 setTimeout,clearTimeout,fetch:async(url,options)=>transport(url,options)};
vm.runInNewContext(fs.readFileSync(process.argv[1].replace('ozon-bulk-repair-vue.js','ozon-workspace-return.js'),'utf8'),context);
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const core=window.ozonBulkRepair;
const field=(id,name,editable=true)=>({external_id:String(id),name,value:'',dictionary_value_id:'',editable,dictionary:false,
 dictionary_fresh:true,data_type:'String',is_collection:false,max_values:1,values:[]});
const row=(id,attributes)=>({draft_id:id,draft_version:1,imported_product_id:id,offer_id:'offer-'+id,title:'Товар '+id,
 action:'ИСПРАВИТЬ',product_type_id:1,product_type_name:'Тип 1',price_rub:'100',description:'',package_width_mm:'',
 package_height_mm:'',package_depth_mm:'',package_weight_g:'',attributes,cleanup_candidates:[],schema_cleanup:false,
 status:'needs_input',validation_errors:[],type_change_impact:{attributes:0,complex_groups:0,removals:0}});
const fields=Array.from({length:105},(_,i)=>field(5000+i,'Свойство '+String(i).padStart(3,'0')));
const first=row(1,fields),second=row(2,[...fields.slice(0,50),field(9000,'Отдельное поле')]),third=row(3,[field(9001,'Поле вне выбора')]);
const editor={job_uid:'synthetic-run',account_id:1,groups:[{id:'g',source_category:'Категория',rows:[first,second,third]}],
 bulk_fields:[{key:'price_rub',label:'Цена продавца, ₽'},{key:'description',label:'Описание'}]};
const config={editor,enabled:true,csrf:'csrf',urls:{read:'/read',apply:'/apply',types:'/types',dictionaries:'/dict/'}};
const options=core.createOptions(config),state=options.data();
for(const [key,fn] of Object.entries(options.methods))state[key]=fn.bind(state);
for(const [key,fn] of Object.entries(options.computed))Object.defineProperty(state,key,{get:()=>fn.call(state)});
state.$nextTick=fn=>fn();state.$refs={modal:{showModal(){},close(){}},error:{focus(){}}};
const response=data=>({status:200,ok:true,redirected:false,json:async()=>({...data,csrf:'fresh-csrf'})});
(async()=>{
 state.rows[0].selected=true;state.rows[1].selected=true;
 assert.equal(state.bulkAttributeChoices.length,106,'picker includes the union of selected editable fields');
 assert(!state.bulkAttributeChoices.some(option=>option.key==='attribute:9001'),'unselected rows do not add field choices');
 assert.equal(state.bulkAttributeChoices.find(option=>option.key==='attribute:5000').count,2);
 assert.equal(state.bulkAttributeWindow.visible.length,40,'the first menu window stays bounded');
 state.bulkKey='attribute:5100';state.bulkFieldQuery='Свойство 000';
 assert.equal(state.bulkAttributeWindow.visible.length,1,'plain name filtering reaches values beyond the first window');
 assert.equal(state.bulkAttributeWindow.visible[0].key,'attribute:5000');
 assert.equal(state.bulkAttributeWindow.pinned.key,'attribute:5100','the current choice stays available outside the search');
 assert.deepEqual(state.selected.map(item=>item.draft_id),[1,2],'field search does not change row selection');
 state.bulkFieldQuery='no match';
 assert.equal(state.bulkAttributeWindow.visible.length,0);assert.equal(state.bulkAttributeWindow.pinned.key,'attribute:5100');
 state.bulkKey='attribute:9001';state.bulkValue='unexpected';state.previewBulk();
 assert.equal(state.bulkPlan,null,'direct preview cannot route to a field outside the selected schema');
 assert.equal(state.bulkKey,'price_rub','stale field selection returns to a current fixed control');
 assert(state.error.includes('больше не доступно'));

 // Page selection is exact and does not change later pages.
 state.bulkFieldQuery='';state.pageSize=2;state.page=1;
 state.rows[0].selected=false;state.rows[1].selected=false;state.rows[2].selected=false;
 state.selectPage({target:{checked:true}});
 assert.equal(state.rows[0].selected,true);assert.equal(state.rows[1].selected,true);assert.equal(state.rows[2].selected,false);
 state.page=2;state.selectPage({target:{checked:true}});assert.equal(state.rows[2].selected,true);
 state.page=1;state.selectPage({target:{checked:false}});
 assert.equal(state.rows[0].selected,false);assert.equal(state.rows[1].selected,false);
 assert.equal(state.rows[2].selected,true,'revisiting one page does not remove another page selection');

 // A scoped read that omits a dirty row keeps its input visible but drops it from selection.
 state.rows[1].description='dirty input to copy';state.rows[1].selected=true;
 const fresh={...editor,groups:[{...editor.groups[0],rows:[first,third]}]};
 transport=async()=>response({success:true,editor:fresh});
 assert.equal(await state.reload({manual:true}),true);
 const unavailable=state.rows.find(item=>item.draft_id===2);
 assert.equal(unavailable.unavailable,true);assert.equal(unavailable.selected,false);
 assert.equal(unavailable.description,'dirty input to copy');
 state.pageSize=1;state.page=state.rows.findIndex(item=>item.draft_id===2)+1;
 assert.equal(state.selectableVisible.length,0);assert.equal(state.pageSelected,false);
 unavailable.selected=true;state.selectPage({target:{checked:false}});
 assert.equal(unavailable.selected,false,'page removal can clear a stale selection');
 state.selectPage({target:{checked:true}});
 assert.equal(unavailable.selected,false,'page selection cannot reselect unavailable rows');
 assert.equal(unavailable.description,'dirty input to copy','page selection never discards stale input');
 state.selectFiltered();
 assert.equal(unavailable.selected,false,'select all by filter continues to skip unavailable rows');
 assert.equal(unavailable.description,'dirty input to copy');
 console.log('mixed bulk field window, filtering, exact selection and stale row retention: passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    template=(Path(__file__).parents[1]/'templates/ozon_bulk_repair.html').read_text()
    assert 'id="orb-bulk-field-query"' in template
    assert 'data-bulk-field-window' in template
    assert 'data-bulk-preview-summary' in template
    assert 'Будет заполнено: {{ bulkPlan.items.length }}' in template
