/* Local repair only. Every save keeps the server's exact scope/version/form gate. */
(function (global) {
    'use strict';
    const clone = value => JSON.parse(JSON.stringify(value));
    const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const BULK_ATTRIBUTE_WINDOW = 40;
    const SOURCE_REVIEW_FIELDS = ['10096','4389'];
    const SOURCE_REVIEW_LABELS = {'10096':'Цвет товара','4389':'Страна-изготовитель'};
    const fixed = ['price_rub','package_width_mm','package_height_mm','package_depth_mm','package_weight_g','description'];
    const labels = {price_rub:'Цена продавца, ₽',package_width_mm:'Ширина упаковки, мм',package_height_mm:'Высота упаковки, мм',package_depth_mm:'Длина упаковки, мм',package_weight_g:'Вес с упаковкой, г',confirm_package:'Подтверждение упаковки',description:'Описание',action:'Действие',product_type_id:'Тип товара',save_mapping:'Запомнить категорию',schema_cleanup:'Очистка несовместимых характеристик'};
    const asText = value => value == null ? '' : String(value);
    const valuesOf = field => {
        const values=asText(field.value).split('\n').filter(Boolean);
        const ids=Array.isArray(field.dictionary_value_ids) ? field.dictionary_value_ids : [];
        return values.map((value,index)=>{
            const id=asText(ids[index] || (values.length===1 ? field.dictionary_value_id : ''));
            return {value,...(id ? {dictionary_value_id:id} : {})};
        });
    };
    const shape = field => JSON.stringify([asText(field.data_type).toLowerCase(),!!field.dictionary,!!field.is_collection,field.max_values || 1]);
    function snapshot(row) {
        const result = Object.fromEntries(fixed.map(key => [key, asText(row[key])]));
        for (const field of row.attributes) result['attribute:'+field.external_id] = clone(field.values);
        return {...result,action:row.action,product_type_id:row.product_type_id,save_mapping:!!row.save_mapping,schema_cleanup:!!row.schema_cleanup,confirm_package:row.confirm_package===true};
    }
    function makeRows(editor) {
        const sourcePrepare = editor.mode === 'source_prepare';
        const draftSelection = editor.mode === 'draft_selection';
        const requested = new Set(draftSelection && Array.isArray(editor.review_draft_ids) ? editor.review_draft_ids : []);
        return editor.groups.flatMap(group => group.rows.map(source => {
            const row = {...clone(source),...(sourcePrepare ? {action:'ИСПРАВИТЬ'} : {}),group_id:group.id,group_name:group.source_category,selected:false,
                expanded:false,save_mapping:false,confirm_package:false,error:'',server:null,dictionaryCopied:false,baseTypeName:source.product_type_name};
            row.source_review_decisions={};
            if (draftSelection && requested.has(row.draft_id)) row.selected=true;
            row.attributes = row.attributes.map(field => ({...field,values:valuesOf(field)}));
            fixed.forEach(key => { row[key] = asText(row[key]); });
            row.base = snapshot(row);
            return row;
        }));
    }
    function changes(row) {
        const current = snapshot(row);
        return Object.keys(current).filter(key => !equal(current[key], row.base[key])).map(key => {
            const before=row.base[key],after=current[key];
            return {
                key, label:labels[key] || row.attributes.find(f => 'attribute:'+f.external_id===key)?.name || 'Характеристика',
                before, after,
                beforeLabel:key==='product_type_id' ? row.baseTypeName || 'Не выбран' : display(before),
                afterLabel:key==='product_type_id' ? row.product_type_name || 'Не выбран' : display(after),
                beforeIdentities:dictionaryIdentities(before),
                afterIdentities:dictionaryIdentities(after),
                showDictionaryIdentities:shouldShowDictionaryIdentities(key,before,after),
            };
        }).filter(change=>{
            if(!change.key.startsWith('attribute:') || !row.source_comparisons)return true;
            const id=change.key.slice(10),field=row.attributes.find(candidate=>String(candidate.external_id)===id);
            return !SOURCE_REVIEW_FIELDS.includes(id) || !field || !sameSourceIdentity(field.values,row.base[change.key]);
        });
    }
    function definition(field) {
        return {...field,id:field.external_id,required:field.required === undefined ? true : !!field.required,
            collection:field.is_collection,max_values:field.max_values || 1};
    }
    function sourceComparisonRows(row) {
        const comparisons=row?.source_comparisons;
        if (!comparisons || typeof comparisons!=='object' || Array.isArray(comparisons)) return [];
        return SOURCE_REVIEW_FIELDS.map(id=>{
            const comparison=comparisons[id];
            return comparison && String(comparison.attribute_id)===id &&
                ['confirmed','unknown'].includes(comparison.status) ? comparison : null;
        }).filter(Boolean);
    }
    function sourceCells(values) {
        if (!Array.isArray(values)) return [];
        return values.map(cell=>({value:asText(cell?.value),dictionary_value_id:asText(cell?.dictionary_value_id)}));
    }
    const normalizedSourceValue = value => asText(value).normalize('NFKC').trim().replace(/\s+/gu,' ');
    function sameSourceIdentity(left,right) {
        const a=sourceCells(left).filter(cell=>cell.dictionary_value_id || normalizedSourceValue(cell.value));
        const b=sourceCells(right).filter(cell=>cell.dictionary_value_id || normalizedSourceValue(cell.value));
        if(a.length!==b.length)return false;
        const matched=new Map();
        const assign=(leftIndex,seen)=>{
            const cell=a[leftIndex];
            for(let rightIndex=0;rightIndex<b.length;rightIndex++) {
                const candidate=b[rightIndex];
                const same=cell.dictionary_value_id && candidate.dictionary_value_id
                    ? cell.dictionary_value_id===candidate.dictionary_value_id
                    : normalizedSourceValue(cell.value)===normalizedSourceValue(candidate.value);
                if(!same || seen.has(rightIndex))continue;
                seen.add(rightIndex);
                if(!matched.has(rightIndex) || assign(matched.get(rightIndex),seen)) {
                    matched.set(rightIndex,leftIndex);return true;
                }
            }
            return false;
        };
        return a.every((_,index)=>assign(index,new Set()));
    }
    function sourceProposalIsBlank(values) {
        const cells=sourceCells(values);
        return !cells.length || cells.every(cell=>!normalizedSourceValue(cell.value));
    }
    function sourceProposalSignature(values) {
        return JSON.stringify(sourceCells(values));
    }
    function canonicalSourceProposal(field) {
        const values=Array.isArray(field?.values) ? field.values : [];
        const submittedText=values.map(value=>asText(value?.value)).join('\n');
        const submittedId=values.length===1 ? asText(values[0]?.dictionary_value_id) : '';
        const lines=submittedText.split(/\r\n|\n|\r/).map(normalizedSourceValue).filter(Boolean);
        const cells=lines.map(value=>({dictionary_value_id:lines.length===1 ? submittedId : '',value}));
        return JSON.stringify(cells.length===1 ? cells[0] : cells);
    }
    async function sha256Hex(value) {
        if (!global.crypto?.subtle?.digest) throw new Error('Браузер не смог подготовить подтверждение значения. Повторите вход в защищённом соединении.');
        const digest=await global.crypto.subtle.digest('SHA-256',new TextEncoder().encode(value));
        return [...new Uint8Array(digest)].map(byte=>byte.toString(16).padStart(2,'0')).join('');
    }
    const encodeCompactDraftIds = values =>
        global.OzonWorkspaceReturn?.encodeCompactDraftIds?.(values) || null;
    function display(value) {
        if (Array.isArray(value)) return value.map(v => asText(v.value)).join(', ') || 'Не заполнено';
        if (value === true) return 'Да';
        if (value === false) return 'Нет';
        if (value === 'ИСКЛЮЧИТЬ') return 'Исключить из повтора';
        if (value === 'ИСПРАВИТЬ') return 'Исправить';
        return asText(value) || 'Не заполнено';
    }
    function dictionaryIdentities(value) {
        if (!Array.isArray(value)) return [];
        return value.map(item => item?.dictionary_value_id)
            .filter(id => typeof id === 'string' && id !== '')
            .map(id => 'ID '+id+' в справочнике Ozon');
    }
    function shouldShowDictionaryIdentities(key,before,after) {
        if (key==='attribute:22232') return true;
        return Array.isArray(before) && Array.isArray(after) &&
            equal(before.map(item => asText(item?.value)),after.map(item => asText(item?.value))) &&
            !equal(dictionaryIdentities(before),dictionaryIdentities(after));
    }
    function shouldShowDictionaryId(externalAttributeId,items) {
        if (asText(externalAttributeId)==='22232') return true;
        if (!Array.isArray(items) || items.length<2) return false;
        const labels=items.map(item => asText(item?.value));
        return new Set(labels).size<labels.length;
    }
    function validSourceComparisons(row) {
        const comparisons=row?.source_comparisons;
        if(comparisons===undefined || comparisons===null)
            return !row.attributes.some(field=>SOURCE_REVIEW_FIELDS.includes(String(field.external_id)) && field.source_managed===true);
        if(typeof comparisons!=='object' || Array.isArray(comparisons) ||
            Object.keys(comparisons).length!==SOURCE_REVIEW_FIELDS.length)return false;
        const expected={'10096':'colors','4389':'country'};
        const validComparisons=SOURCE_REVIEW_FIELDS.every(id=>{
            const comparison=comparisons[id];
            if(!comparison || String(comparison.attribute_id)!==id || comparison.complex_id!=='0' ||
                comparison.source_field!==expected[id] || !['confirmed','unknown'].includes(comparison.status) ||
                !(comparison.reason===null || typeof comparison.reason==='string' && comparison.reason.length<=80) ||
                !(comparison.reason_label===null || typeof comparison.reason_label==='string' && comparison.reason_label.length<=240) ||
                !Array.isArray(comparison.literal_labels) || comparison.literal_labels.length>20 ||
                !comparison.literal_labels.every(value=>typeof value==='string' && value.length<=500) ||
                !Array.isArray(comparison.resolved_values) || comparison.resolved_values.length>20 ||
                !comparison.resolved_values.every(value=>value && typeof value.value==='string' && value.value.length<=500 &&
                    typeof value.dictionary_value_id==='string' && value.dictionary_value_id.length<=120) ||
                !(comparison.evidence_receipt===null || typeof comparison.evidence_receipt==='string' &&
                    comparison.evidence_receipt.length>0 && comparison.evidence_receipt.length<=4096))return false;
            if(comparison.status==='unknown' && (comparison.literal_labels.length || comparison.resolved_values.length))return false;
            if(comparison.resolved_values.length && (!comparison.evidence_receipt || comparison.status!=='confirmed'))return false;
            return true;
        });
        if(!validComparisons)return false;
        return row.attributes.filter(field=>SOURCE_REVIEW_FIELDS.includes(String(field.external_id)) && field.source_managed===true)
            .every(field=>typeof field.required==='boolean' && Array.isArray(field.dictionary_value_ids) &&
                field.dictionary_value_ids.length===asText(field.value).split('\n').filter(Boolean).length &&
                field.dictionary_value_ids.every(id=>typeof id==='string' && id.length<=120) &&
                (field.dictionary_value_id===undefined || typeof field.dictionary_value_id==='string'));
    }
    function serialize(rows, csrf, sourcePrepare=false, draftSelection=false, sourceReviewJSON={}) {
        const form = new URLSearchParams();
        form.append('csrf_token',csrf);
        if (draftSelection) form.append('confirm_local','1');
        const nativeSourceReview=sourcePrepare || draftSelection;
        for (const row of rows) {
            const prefix = 'row_'+row.draft_id+'_';
            form.append('selected_draft_id',String(row.draft_id));
            for (const [key,value] of Object.entries({draft_version:row.draft_version,imported_product_id:row.imported_product_id,
                action:sourcePrepare || draftSelection ? 'ИСПРАВИТЬ' : row.action,product_type_id:row.product_type_id || '',save_mapping:!draftSelection && row.save_mapping ? '1':'0'})) form.append(prefix+key,asText(value));
            fixed.forEach(key => form.append(prefix+key,asText(row[key])));
            if(row.confirm_package===true)form.append(prefix+'confirm_package','1');
            const typeChanged = row.product_type_id !== row.base.product_type_id;
            if (row.cleanup_candidates.length && row.schema_cleanup && !typeChanged) form.append(prefix+'schema_cleanup','1');
            for (const field of row.attributes.filter(f => f.editable)) {
                const sourceField=nativeSourceReview && field.source_managed===true && SOURCE_REVIEW_FIELDS.includes(String(field.external_id));
                if (sourceField && (typeChanged || row.source_review_decisions?.[String(field.external_id)]?.action==='exclude' ||
                    sameSourceIdentity(field.values,row.base['attribute:'+field.external_id]))) continue;
                const values = typeChanged ? [] : field.values;
                form.append(prefix+'attribute_'+field.external_id,values.map(v => v.value).join('\n'));
                form.append(prefix+'attribute_value_id_'+field.external_id,values.length === 1 ? values[0].dictionary_value_id || '' : '');
            }
            if (nativeSourceReview && sourceReviewJSON[row.draft_id])
                form.append(prefix+'source_review_json',String(sourceReviewJSON[row.draft_id]));
        }
        return form;
    }
    function planBulk(rows, key, value, sourceField, sourceTypeId=null) {
        const items = [], skipped = [];
        for (const row of rows) {
            let reason = row.unavailable ? 'Карточка больше недоступна в этом запуске' :
                row.action === 'ИСКЛЮЧИТЬ' ? 'Карточка исключается из повтора' :
                row.server ? 'Сначала разрешите конфликт' : '';
            const field = row.attributes.find(f => 'attribute:'+f.external_id===key);
            if (!reason && key.startsWith('attribute:')) {
                if (row.product_type_id !== row.base.product_type_id) reason='Сначала сохраните новый тип';
                else if (!field?.editable) reason='Поле недоступно в этой карточке';
                else if (field.dictionary && !field.dictionary_fresh) reason='Справочник требует обновления';
                else if (!sourceField || shape(field)!==shape(sourceField)) reason='Другая форма поля или ограничение числа значений';
                else if (value.length > field.max_values) reason='Слишком много значений';
            } else if (!reason && key==='schema_cleanup' && !row.cleanup_candidates.length) reason='Нет характеристик для очистки';
            else if (!reason && !fixed.includes(key) && key!=='schema_cleanup' && !key.startsWith('attribute:')) reason='Поле недоступно';
            if (reason) { skipped.push({id:row.draft_id,title:row.title,reason}); continue; }
            const preserveExactDictionaryId = !!field?.dictionary && !!sourceField?.dictionary &&
                Number.isSafeInteger(sourceTypeId) && sourceTypeId > 0 &&
                row.base.product_type_id === sourceTypeId &&
                Array.isArray(value) && value.length === 1 &&
                typeof value[0]?.dictionary_value_id === 'string' && value[0].dictionary_value_id !== '';
            const next = Array.isArray(value) ? value.map(v => ({value:v.value,
                ...(preserveExactDictionaryId ? {dictionary_value_id:value[0].dictionary_value_id} : {})})) : value;
            const before = field ? field.values : row[key];
            const exactValuesMatch = equal(before,next);
            const dictionaryValueMatches = field?.dictionary && Array.isArray(before) && Array.isArray(next) &&
                (preserveExactDictionaryId
                    ? exactValuesMatch
                    : equal(before.map(v=>v.value),next.map(v=>v.value)));
            if (exactValuesMatch || dictionaryValueMatches) { skipped.push({id:row.draft_id,title:row.title,reason:'Значение уже совпадает'}); continue; }
            items.push({id:row.draft_id,title:row.title,before:clone(before),after:clone(next)});
        }
        return {key,items,skipped};
    }
    function validEditor(value, config) {
        if (!value || value.account_id!==config.editor.account_id || value.job_uid!==config.editor.job_uid ||
            !Array.isArray(value.groups) || !value.groups.every(g => Array.isArray(g.rows)) ||
            value.groups.reduce((n,g) => n+g.rows.length,0)>200) return false;
        const rows=value.groups.flatMap(g => g.rows);
        if (!rows.every(r => Number.isSafeInteger(r.draft_id) && r.draft_id>0 &&
            Number.isSafeInteger(r.draft_version) && r.draft_version>0 && Array.isArray(r.attributes))) return false;
        if(['source_prepare','draft_selection'].includes(config.editor.mode) &&
            !rows.every(r => typeof r.source_refresh_available === 'boolean')) return false;
        if(['source_prepare','draft_selection'].includes(config.editor.mode) && !rows.every(validSourceComparisons))return false;
        if (config.editor.mode==='source_prepare') {
            const expected=value.review_draft_ids,blocked=value.blocked_review_items,rowIds=rows.map(row=>row.draft_id);
            if (value.mode!=='source_prepare' || !Array.isArray(expected) || !Array.isArray(blocked) ||
                !expected.every(id=>Number.isSafeInteger(id) && id>0) ||
                !blocked.every(item=>item && Number.isSafeInteger(item.draft_id) && item.draft_id>0 &&
                    typeof item.title==='string' && typeof item.message==='string')) return false;
            const expectedSet=new Set(expected);
            if (expectedSet.size!==expected.length || rowIds.length!==expected.length ||
                new Set(rowIds).size!==expectedSet.size || rowIds.some(id=>!expectedSet.has(id))) return false;
        }
        if (config.editor.mode==='draft_selection') {
        const expectedRows=config.editor.groups.flatMap(group=>group.rows).map(row=>row.draft_id);
            const expectedBlocked=config.editor.blocked_review_items || [];
            const expectedIds=[...expectedRows,...expectedBlocked.map(item=>item.draft_id)];
            const reviewIds=value.review_draft_ids,blocked=value.blocked_review_items,rowIds=rows.map(row=>row.draft_id);
            if (value.mode!=='draft_selection' || value.scope_key!==config.editor.scope_key ||
                typeof value.scope_key!=='string' || !/^[0-9a-f]{64}$/.test(value.scope_key) ||
                !Array.isArray(reviewIds) || reviewIds.length>200 || !reviewIds.every(id=>Number.isSafeInteger(id) && id>0) ||
                new Set(reviewIds).size!==reviewIds.length || !Array.isArray(blocked) || blocked.length>200 ||
                !blocked.every(item=>item && Number.isSafeInteger(item.draft_id) && item.draft_id>0 &&
                    typeof item.title==='string' && typeof item.message==='string')) return false;
            const expectedSet=new Set(expectedIds),rowSet=new Set(rowIds),reviewSet=new Set(reviewIds),blockedIds=blocked.map(item=>item.draft_id),blockedSet=new Set(blockedIds);
            if (!expectedIds.length || expectedIds.length>200 || expectedSet.size!==expectedIds.length ||
                rowSet.size!==rowIds.length || reviewSet.size!==reviewIds.length ||
                blockedSet.size!==blockedIds.length || reviewSet.size!==rowSet.size ||
                rowIds.some(id=>!reviewSet.has(id)) ||
                blockedIds.some(id=>!expectedSet.has(id) || rowSet.has(id)) ||
                [...rowSet,...blockedSet].some(id=>!expectedSet.has(id)) ||
                rowSet.size+blockedSet.size!==expectedSet.size) return false;
        }
        return true;
    }
    function createOptions(config) {
        let alive=true, controller=null, searchController=null, searchTimer=null, searchRevision=0, requestRevision=0, unload=null, pop=null, trigger=null;
        const sourcePrepare=config.editor.mode==='source_prepare';
        const draftSelection=config.editor.mode==='draft_selection';
        const selectionScope=String(config.editor.scope_key || 'invalid').slice(0,512);
        const storageKey='ozon-repair:'+config.editor.account_id+':' +
            (draftSelection ? 'draft-selection:'+encodeURIComponent(selectionScope) :
                config.editor.job_uid+(sourcePrepare?':source_prepare':''));
        function scopedUrl(base, query, suffix='') {
            const url=new URL(base,global.location.origin);
            url.searchParams.set('q',query);
            const extra=String(suffix || '').replace(/^[?&]+/,'');
            if(extra){
                const params=new URLSearchParams(extra);
                for(const [key,value] of params.entries()){
                    if(key==='q')continue;
                    url.searchParams.set(key,value);
                }
            }
            return url.pathname+(url.search ? url.search : '');
        }
        return {
            components:{'ozon-field':global.ozonDraftEditor.fieldComponent,'ozon-photo':global.ozonDraftEditor.photoComponent},
            data() { return {rows:makeRows(config.editor),enabled:config.enabled,sourcePrepare,draftSelection,urls:config.urls,
                reviewable:!!config.editor.review_draft_ids?.length,blockedReviewItems:config.editor.blocked_review_items || [],
                blockedReviewCount:config.editor.blocked_review_items?.length || 0,busy:false,error:'',notice:'',
                report:null,sourceRefreshReview:[],sourceRefreshResult:null,uncertain:false,sessionExpired:false,query:'',group:'',state:'',page:1,pageSize:15,dialog:'',
                bulkKey:config.editor.bulk_fields.find(field=>!field.key.startsWith('attribute:'))?.key || 'description',
                bulkFieldQuery:'',bulkValue:'',bulkValues:[],bulkMode:'value',bulkSource:null,bulkPlan:null,
                fields:config.editor.bulk_fields,picker:{kind:'',rowId:null,fieldId:null,query:'',items:[],loading:false,error:'',more:false},
                typeChoice:null,typeIds:[],rememberType:false,review:[],conflictId:null}; },
            computed:{
                selected() { return this.rows.filter(r => r.selected); },
                sourceRefreshTargets() {
                    return (sourcePrepare || draftSelection) ? this.selected.filter(row =>
                        row.source_refresh_available === true && !row.unavailable && !row.server) : [];
                },
                groups() { return [...new Map(this.rows.map(r => [r.group_id,{id:r.group_id,name:r.group_name}])).values()]; },
                filtered() {
                    const term=this.query.trim().normalize('NFKC').toLocaleLowerCase('ru-RU');
                    return this.rows.filter(r => (!this.group || r.group_id===this.group) &&
                        (!this.state || (this.state==='changed' ? changes(r).length : this.state==='error' ? r.error || r.repair_error || r.server : r.status===this.state)) &&
                        (!term || (r.title+' '+r.offer_id).normalize('NFKC').toLocaleLowerCase('ru-RU').includes(term)));
                },
                pages() { return Math.max(1,Math.ceil(this.filtered.length/this.pageSize)); },
                visible() { return this.filtered.slice((this.page-1)*this.pageSize,this.page*this.pageSize); },
                selectableVisible() { return this.visible.filter(r=>!r.unavailable); },
                pageSelected() { return this.selectableVisible.length>0 && this.selectableVisible.every(r=>r.selected); },
                dirty() { return this.rows.some(r => changes(r).length); },
                locked() { return this.busy || this.uncertain || this.sessionExpired || !this.enabled; },
                bulkFixedFields() { return this.fields.filter(field=>!field.key.startsWith('attribute:')); },
                bulkAttributeChoices() {
                    const choices=new Map();
                    for(const row of this.selected) {
                        if(!this.bulkRowUsable(row) || this.changedType(row))continue;
                        const seen=new Set();
                        for(const field of row.attributes) {
                            if(!field.editable || field.dictionary && !field.dictionary_fresh)continue;
                            const key='attribute:'+field.external_id;
                            if(seen.has(key))continue;
                            seen.add(key);
                            const option=choices.get(key) || {key,label:field.name || 'Характеристика',field,sourceRow:row,count:0};
                            option.count++;
                            choices.set(key,option);
                        }
                    }
                    return [...choices.values()].sort((a,b)=>a.label.localeCompare(b.label,'ru-RU',{sensitivity:'base'}) ||
                        a.key.localeCompare(b.key,'ru-RU'));
                },
                bulkAttributeWindow() {
                    const term=this.bulkFieldQuery.trim().normalize('NFKC').toLocaleLowerCase('ru-RU');
                    const matching=this.bulkAttributeChoices.filter(option=>!term ||
                        option.label.normalize('NFKC').toLocaleLowerCase('ru-RU').includes(term));
                    const visible=matching.slice(0,BULK_ATTRIBUTE_WINDOW);
                    const selected=this.bulkAttributeChoices.find(option=>option.key===this.bulkKey) || null;
                    const pinned=selected && !visible.some(option=>option.key===selected.key) ? selected : null;
                    return {matchingCount:matching.length,total:this.bulkAttributeChoices.length,visible,pinned};
                },
                bulkSources() {
                    return this.selected.filter(row=>this.bulkRowUsable(row) &&
                        (!this.bulkKey.startsWith('attribute:') || row.attributes.some(field=>
                            'attribute:'+field.external_id===this.bulkKey && field.editable &&
                            (!field.dictionary || field.dictionary_fresh))));
                },
                bulkFieldOption() { return this.bulkAttributeChoices.find(option=>option.key===this.bulkKey) || null; },
                bulkField() { return this.bulkFieldOption?.field || null; },
                bulkFieldSourceRow() { return this.bulkFieldOption?.sourceRow || null; },
                conflictRow() { return this.rows.find(r => r.draft_id===this.conflictId); },
                sourceReviewAvailable() { return (this.sourcePrepare || this.draftSelection) &&
                    this.review.some(item=>this.sourceComparisonsFor(item.id).length>0); },
                sourceDecisionsResolved() {
                    return this.review.every(item=>{
                        const row=this.rows.find(candidate=>candidate.draft_id===item.id);
                        return sourceComparisonRows(row).every(comparison=>!this.sourceDecisionRequired(item.id,comparison) ||
                            ['acknowledged','excluded'].includes(this.sourceDecisionState(item.id,comparison)));
                    });
                },
                genericReviewItems() { return this.review.filter(item=>this.showGenericReviewItem(item)); },
                genericReviewHasHeading() { return this.sourceReviewAvailable && this.genericReviewItems.some(item=>
                    this.sourceComparisonsFor(item.id).length && this.visibleReviewChanges(item).length); },
                loginUrl() { return '/login?next='+encodeURIComponent(global.location.pathname+global.location.search); },
            },
            methods:{
                display,dictionaryIdentities,shouldShowDictionaryIdentities,shouldShowDictionaryId,definition,changes,
                packageConfirmationNeeded(row) {
                    const confirmation=row?.package_confirmation;
                    if(!confirmation || confirmation.source==='linked_listing')return false;
                    if(confirmation.required===true)return true;
                    return fixed.slice(1,5).some(key=>asText(row[key])!==asText(row.base?.[key]));
                },
                packageConfirmationSourceLabel(row) {
                    const source=row?.package_confirmation?.source;
                    if(source==='legacy_wb_unverified') {
                        return 'Значения из карточки WB. Сверьте с фактической упаковкой.';
                    }
                    if(source==='unverified')return 'Сверьте текущие значения с фактической упаковкой.';
                    return 'Сверьте полный комплект габаритов и веса с фактической упаковкой.';
                },
                packageConfirmationComplete(row) {
                    return fixed.slice(1,5).every(key=>{
                        const value=asText(row?.[key]).trim();
                        if(!/^\d+$/.test(value))return false;
                        const number=Number(value);
                        return Number.isSafeInteger(number) && number>0 && number<=2147483647;
                    });
                },
                packageConfirmationSuggestionText(row) {
                    const value=row?.package_confirmation?.suggested_dimensions;
                    return value ? `Подсказка из WB: ${value.width} × ${value.height} × ${value.depth} мм; ${value.weight} г` : '';
                },
                showPackageConfirmationSuggestion(row) {
                    const value=row?.package_confirmation?.suggested_dimensions;
                    if(!value)return false;
                    return [
                        ['package_width_mm','width'],['package_height_mm','height'],
                        ['package_depth_mm','depth'],['package_weight_g','weight'],
                    ].some(([field,key])=>Number(row[field])!==Number(value[key]));
                },
                usePackageSuggestion(row) {
                    const value=row?.package_confirmation?.suggested_dimensions;
                    if(!value || this.locked)return;
                    row.package_width_mm=asText(value.width);
                    row.package_height_mm=asText(value.height);
                    row.package_depth_mm=asText(value.depth);
                    row.package_weight_g=asText(value.weight);
                    this.packageDimensionsChanged(row);
                },
                packageDimensionsChanged(row) {
                    if(row.confirm_package)row.confirm_package=false;
                    this.noteChange(row);
                },
                sourceComparisonsFor(draftId) {
                    const row=this.rows.find(candidate=>candidate.draft_id===draftId);
                    if(!row || this.changedType(row))return [];
                    return sourceComparisonRows(row).filter(comparison=>{
                        const decision=row.source_review_decisions?.[String(comparison.attribute_id)];
                        if(decision?.action==='acknowledge' || decision?.action==='exclude')return true;
                        const field=row.attributes.find(candidate=>String(candidate.external_id)===String(comparison.attribute_id) && candidate.editable && candidate.source_managed===true);
                        return !!field && !sameSourceIdentity(field.values,row.base['attribute:'+comparison.attribute_id]);
                    });
                },
                sourceFieldLabel(draftId,comparison) {
                    const row=this.rows.find(candidate=>candidate.draft_id===draftId);
                    const field=row?.attributes.find(candidate=>String(candidate.external_id)===String(comparison.attribute_id));
                    return field?.name || SOURCE_REVIEW_LABELS[String(comparison.attribute_id)] || 'Характеристика';
                },
                sourceEvidenceStatusLabel(comparison) {
                    if(comparison.status!=='confirmed')return 'Нет подтверждённых данных';
                    if(!Array.isArray(comparison.resolved_values) || !comparison.resolved_values.length)
                        return 'Источник подтверждён; нет точного значения Ozon';
                    if(!comparison.evidence_receipt)return 'Источник подтверждён; проверка недоступна';
                    return 'Подтверждено';
                },
                sourceValuesLabel(values) {
                    const labels=(Array.isArray(values) ? values : []).map(asText).filter(value=>value!=='');
                    return labels.length ? labels.join(', ') : 'Не задано';
                },
                sourceSavedValues(draftId,comparison) {
                    const row=this.rows.find(candidate=>candidate.draft_id===draftId);
                    return row?.base?.['attribute:'+comparison.attribute_id] || [];
                },
                sourceProposalValues(draftId,comparison) {
                    const row=this.rows.find(candidate=>candidate.draft_id===draftId);
                    const decision=row?.source_review_decisions?.[String(comparison.attribute_id)];
                    if(decision?.action==='exclude' || decision?.action==='acknowledge')return decision.proposal;
                    return row?.attributes.find(field=>String(field.external_id)===String(comparison.attribute_id))?.values || [];
                },
                sourceCurrentLabel(draftId,comparison) {
                    const values=sourceCells(this.sourceSavedValues(draftId,comparison));
                    return values.length ? values.map(cell=>cell.value || 'Не задано').join(', ') : 'Не задано';
                },
                sourceCurrentIdentities(draftId,comparison) {
                    return dictionaryIdentities(this.sourceSavedValues(draftId,comparison));
                },
                sourceResolvedIdentities(comparison) {
                    return dictionaryIdentities(comparison.resolved_values);
                },
                sourceHasDictionaryIds(draftId,comparison) {
                    return this.sourceCurrentIdentities(draftId,comparison).length>0 ||
                        this.sourceResolvedIdentities(comparison).length>0 || this.sourceProposedIdentities(draftId,comparison).length>0;
                },
                sourceShouldShowIds(draftId,comparison) {
                    const arrays=[this.sourceSavedValues(draftId,comparison),comparison.resolved_values,
                        this.sourceProposalValues(draftId,comparison)].map(sourceCells);
                    for(const cells of arrays) {
                        const labels=cells.map(cell=>normalizedSourceValue(cell.value));
                        if(new Set(labels).size<labels.length)return true;
                    }
                    const sorted=values=>values.map(sourceCells).map(cells=>({
                        labels:cells.map(cell=>normalizedSourceValue(cell.value)).sort(),
                        ids:cells.map(cell=>cell.dictionary_value_id).sort(),
                    }));
                    const rows=sorted([this.sourceSavedValues(draftId,comparison),comparison.resolved_values,
                        this.sourceProposalValues(draftId,comparison)]);
                    for(let left=0;left<rows.length;left++)for(let right=left+1;right<rows.length;right++) {
                        if(equal(rows[left].labels,rows[right].labels) && !equal(rows[left].ids,rows[right].ids))return true;
                    }
                    return false;
                },
                sourceProposedLabel(draftId,comparison) {
                    const row=this.rows.find(candidate=>candidate.draft_id===draftId);
                    if(!row)return 'Не задано';
                    if(this.changedType(row))return 'Значение нужно сверить после выбора типа';
                    const field=row.attributes.find(candidate=>String(candidate.external_id)===String(comparison.attribute_id));
                    if(!field)return 'Поле отсутствует в выбранном типе';
                    const values=sourceCells(this.sourceProposalValues(draftId,comparison));
                    return values.length ? values.map(cell=>cell.value || 'Не задано').join(', ') : 'Не задано';
                },
                sourceProposedIsBlank(draftId,comparison) {
                    return sourceProposalIsBlank(this.sourceProposalValues(draftId,comparison));
                },
                sourceProposedIdentities(draftId,comparison) {
                    return dictionaryIdentities(this.sourceProposalValues(draftId,comparison));
                },
                sourceDecisionRequired(draftId,comparison) {
                    const row=this.rows.find(candidate=>candidate.draft_id===draftId);
                    if(!row || !(this.sourcePrepare || this.draftSelection))return false;
                    const id=String(comparison.attribute_id),decision=row.source_review_decisions?.[id];
                    if(decision?.action==='acknowledge' || decision?.action==='exclude')return true;
                    if(this.changedType(row) || comparison.status!=='confirmed' ||
                        !Array.isArray(comparison.resolved_values) || !comparison.resolved_values.length)return false;
                    const field=row.attributes.find(candidate=>String(candidate.external_id)===id && candidate.editable && candidate.source_managed===true);
                    if(!field)return false;
                    const saved=row.base?.['attribute:'+id] || [];
                    const proposed=field.values;
                    if(sourceProposalIsBlank(proposed))return false;
                    return !sameSourceIdentity(proposed,saved) && !sameSourceIdentity(proposed,comparison.resolved_values);
                },
                sourceDecisionState(draftId,comparison) {
                    const row=this.rows.find(candidate=>candidate.draft_id===draftId);
                    const action=row?.source_review_decisions?.[String(comparison.attribute_id)]?.action;
                    if(action==='acknowledge')return 'acknowledged';
                    if(action==='exclude')return 'excluded';
                    return this.sourceDecisionRequired(draftId,comparison) ? 'unresolved' : 'none';
                },
                visibleReviewChanges(item) {
                    return item.changes.filter(change=>{
                        if(!change.key.startsWith('attribute:'))return true;
                        const id=change.key.slice(10);
                        const sourceField=SOURCE_REVIEW_FIELDS.includes(id);
                        if(sourceField && this.sourceComparisonsFor(item.id).some(comparison=>String(comparison.attribute_id)===id))return false;
                        return this.sourceDecisionState(item.id,{attribute_id:id})!=='excluded';
                    });
                },
                showGenericReviewItem(item) {
                    if(item.action==='ИСКЛЮЧИТЬ' || item.typeChanged)return true;
                    if(this.visibleReviewChanges(item).length)return true;
                    return !this.sourceComparisonsFor(item.id).length && !item.changes.length;
                },
                clearSourceDecision(row,attributeId) {
                    const id=String(attributeId);
                    if(SOURCE_REVIEW_FIELDS.includes(id) && row.source_review_decisions)
                        delete row.source_review_decisions[id];
                },
                setSourceDecision(draftId,comparison,action) {
                    if(this.locked || !['acknowledge','exclude'].includes(action))return;
                    const row=this.rows.find(candidate=>candidate.draft_id===draftId);
                    const id=String(comparison.attribute_id),field=row?.attributes.find(candidate=>String(candidate.external_id)===id);
                    if(!row || !field || !this.sourceDecisionRequired(draftId,comparison))return;
                    const prior=row.source_review_decisions?.[id];
                    const proposed=(prior?.action==='exclude' || prior?.action==='acknowledge')
                        ? clone(prior.proposal) : clone(field.values);
                    if(action==='acknowledge' && !comparison.evidence_receipt) {
                        this.error='Нет действующего подтверждения источника. Обновите состояние карточки или исключите это поле.';
                        return;
                    }
                    row.source_review_decisions=row.source_review_decisions || {};
                    row.source_review_decisions[id]={action,proposal:proposed,signature:sourceProposalSignature(proposed)};
                    field.values=action==='exclude' ? clone(row.base['attribute:'+id] || []) : clone(proposed);
                    row.error='';this.error='';
                },
                async sourceReviewJSON(rows) {
                    const payload={};
                    for(const row of rows) {
                        const entries={};
                        for(const comparison of sourceComparisonRows(row)) {
                            const id=String(comparison.attribute_id),decision=row.source_review_decisions?.[id];
                            if(decision?.action==='exclude')continue;
                            const field=row.attributes.find(candidate=>String(candidate.external_id)===id && candidate.editable && candidate.source_managed===true);
                            if(!field || this.changedType(row) || sourceProposalIsBlank(field.values) ||
                                sameSourceIdentity(field.values,row.base['attribute:'+id]))continue;
                            if(decision?.action==='acknowledge' && sourceProposalSignature(field.values)!==decision.signature) {
                                delete row.source_review_decisions[id];
                                throw new Error('Предложение изменилось. Сверьте это поле ещё раз.');
                            }
                            if(comparison.status!=='confirmed' || !comparison.resolved_values?.length || !comparison.evidence_receipt)continue;
                            const conflicts=!sameSourceIdentity(field.values,comparison.resolved_values);
                            if(conflicts && decision?.action!=='acknowledge')
                                throw new Error('Для этого отличающегося предложения нужно подтвердить значение или исключить поле.');
                            entries[id]={receipt:String(comparison.evidence_receipt),ack:conflicts,proposal_sha256:await sha256Hex(canonicalSourceProposal(field))};
                        }
                        if(Object.keys(entries).length)payload[row.draft_id]=JSON.stringify(entries);
                    }
                    return payload;
                },
                status(row) { return row.server ? 'Конфликт изменений' : row.error || row.repair_error ? 'Не сохранено' :
                    !this.draftSelection && row.action==='ИСКЛЮЧИТЬ' ? 'Исключена из повтора' : changes(row).length ? 'Есть изменения' :
                    row.status==='needs_input' && this.sourcePrepare ? 'Нужны данные' :
                    row.phase==='prepared' ? 'Подготовлена локально' : row.status==='ready_to_retry' ? 'Проверена локально' : 'Нужно исправить'; },
                changedType(row) { return row.product_type_id!==row.base.product_type_id; },
                changeField(row,field,values) { this.clearSourceDecision(row,field.external_id);field.values=clone(values);row.error=''; },
                noteChange(row) { row.error=''; },
                setFilters() { this.page=1;this.syncURL(); },
                bulkRowUsable(row) { return !row.unavailable && !row.server && row.action!=='ИСКЛЮЧИТЬ'; },
                syncBulkField() {
                    const fixed=this.bulkFixedFields.find(field=>field.key===this.bulkKey);
                    const attribute=this.bulkAttributeChoices.find(field=>field.key===this.bulkKey);
                    if(fixed || attribute)return true;
                    this.bulkKey=this.bulkFixedFields[0]?.key || 'description';
                    this.resetBulk();
                    return false;
                },
                selectionChanged() {
                    if(this.dialog!=='bulk')return;
                    this.syncBulkField();
                    this.resetBulk();
                },
                syncURL() {
                    const url=new URL(global.location.href);
                    for (const [key,value] of Object.entries({q:this.query,group:this.group,state:this.state,page:this.page>1 ? this.page : ''})) {
                        if (value) url.searchParams.set(key,String(value));else url.searchParams.delete(key);
                    }
                    global.history.replaceState(null,'',url.pathname+url.search+url.hash);
                },
                fromURL() { const args=new URL(global.location.href).searchParams;this.query=(args.get('q') || '').slice(0,200);
                    this.group=args.get('group') || '';this.state=args.get('state') || '';if(this.sourcePrepare && this.state==='excluded')this.state='';this.page=Math.max(1,Math.min(this.pages,Number(args.get('page')) || 1)); },
                goPage(page) { this.page=Math.max(1,Math.min(page,this.pages));this.syncURL(); },
                persist() { try { global.sessionStorage.setItem(storageKey,JSON.stringify({selected:this.selected.map(r=>r.draft_id),uncertain:this.uncertain})); } catch (_) {} },
                selectPage(event) {
                    const checked=!!event.target.checked;
                    for(const row of this.visible) {
                        if(!checked || !row.unavailable)row.selected=checked;
                    }
                    this.persist();this.selectionChanged();
                },
                selectFiltered() { this.filtered.filter(r=>!r.unavailable).forEach(r=>{r.selected=true;});this.persist();this.selectionChanged(); },
                clearSelection() { this.rows.forEach(r => {r.selected=false;});this.persist();this.selectionChanged(); },
                selectRow(row) { if(row.unavailable)return;row.selected=!row.selected;this.persist();this.selectionChanged(); },
                open(name) { trigger=global.document.activeElement;this.dialog=name;this.$nextTick(()=>this.$refs.modal.showModal()); },
                close({restoreFocus=true}={}) { if(this.busy)return;this.cancelSearch();if(this.dialog==='source_refresh')this.sourceRefreshReview=[];this.dialog='';this.$refs.modal.close();if(restoreFocus)trigger?.focus(); },
                cancelSearch() { clearTimeout(searchTimer);searchRevision++;searchController?.abort();this.picker.loading=false; },
                async request(url, options={}) {
                    let response;
                    try {response=await fetch(url,{credentials:'same-origin',headers:{Accept:'application/json','X-CSRFToken':config.csrf},...options});}
                    catch(error) {if(error.name==='AbortError')throw error;throw new Error('Нет ответа от сервера. Проверьте соединение; введённые данные остаются в форме.');}
                    if(response.status===401 || response.redirected && new URL(response.url).pathname==='/login') {
                        this.sessionExpired=true;throw Object.assign(new Error('Сессия закончилась. Войдите снова; введённые данные остаются на этой странице.'),{known:true,status:401});
                    }
                    let data;try{data=await response.json();}catch(_){throw new Error('Не удалось подтвердить ответ сервера.');}
                    if(!response.ok || !data.success) throw Object.assign(new Error(asText(data.error || 'Не удалось выполнить запрос.').slice(0,500)),{known:response.status>=400 && response.status<500,status:response.status});
                    return data;
                },
                async reload({report=null,manual=false}={}) {
                    if(this.busy && manual)return false;
                    let confirmed=false;
                    this.busy=true;this.error='';const revision=++requestRevision;controller?.abort();controller=new AbortController();
                    const readControl=controller,timeout=setTimeout(()=>readControl.abort(),30000);
                    try {
                        const data=await this.request(config.urls.read,{signal:controller.signal});
                        if(!alive || revision!==requestRevision)return false;
                        if(!validEditor(data.editor,config))throw new Error('Не удалось подтвердить магазин и состав загрузки.');
                        if(typeof data.csrf!=='string' || !data.csrf || data.csrf.length>512)throw new Error('Не удалось обновить защиту формы. Повторите чтение состояния.');
                        config.csrf=data.csrf;
                        const fresh=makeRows(data.editor),old=new Map(this.rows.map(r=>[r.draft_id,r]));
                        const outcomes=new Map((report?.rows || []).map(r=>[r.draft_id,r]));
                        this.rows=fresh.map(server=>{
                            const row=old.get(server.draft_id);if(!row)return server;
                            if(!equal(row.source_comparisons,server.source_comparisons))row.source_review_decisions={};
                            row.source_comparisons=clone(server.source_comparisons || {});
                            server.selected=row.selected;server.expanded=row.expanded;
                            const result=outcomes.get(row.draft_id),edited=changes(row).length>0;
                            if(result && result.status!=='failed' && result.version===server.draft_version)return server;
                            if(edited && equal(snapshot(row),snapshot(server)))return server;
                            if(edited || result?.status==='failed') {
                                row.error=result?.message || row.error;
                                if(row.draft_version!==server.draft_version || !equal(row.attributes.map(shape),server.attributes.map(shape)) ||
                                    !equal(row.attributes.map(f=>f.external_id),server.attributes.map(f=>f.external_id))) row.server=server;
                                else {row.validation_errors=server.validation_errors;row.repair_error=server.repair_error;row.status=server.status;}
                                return row;
                            }
                            return server;
                        });
                        // Missing rows remain visible for explicit conflict resolution; never silently discard input.
                        for(const row of old.values())if(!fresh.some(r=>r.draft_id===row.draft_id) && changes(row).length) {
                            row.error='Карточка больше недоступна в этом запуске. Ввод сохранён для копирования.';
                            row.unavailable=true;row.selected=false;this.rows.push(row);
                        }
                        this.fields=data.editor.bulk_fields;this.syncBulkField();this.resetBulk();this.blockedReviewItems=data.editor.blocked_review_items || [];
                        this.blockedReviewCount=this.blockedReviewItems.length;
                        this.reviewable=!!data.editor.review_draft_ids?.length;
                        if(this.reviewable && this.urls.review && this.sourcePrepare){const reviewUrl=new URL(this.urls.review,global.location.origin);
                            reviewUrl.searchParams.set('account_id',String(data.editor.account_id));
                            reviewUrl.searchParams.set('draft_ids',data.editor.review_draft_ids.join(','));
                            reviewUrl.searchParams.set('parent_prepare_job_uid',data.editor.job_uid);
                            this.urls.review=reviewUrl.pathname+reviewUrl.search;}
                        if(this.reviewable && this.urls.review && this.draftSelection){const reviewUrl=new URL(this.urls.review,global.location.origin);
                            reviewUrl.searchParams.set('account_id',String(data.editor.account_id));
                            if(reviewUrl.searchParams.has('workspace_selection')){
                                const compact=encodeCompactDraftIds(data.editor.review_draft_ids);
                                if(!compact)throw new Error('Не удалось сохранить точный короткий список черновиков.');
                                reviewUrl.searchParams.delete('draft_ids');
                                reviewUrl.searchParams.set('workspace_selection',compact);
                            }else{
                                reviewUrl.searchParams.delete('workspace_selection');
                                reviewUrl.searchParams.set('draft_ids',data.editor.review_draft_ids.join(','));
                            }
                            reviewUrl.searchParams.delete('parent_prepare_job_uid');
                            this.urls.review=reviewUrl.pathname+reviewUrl.search+reviewUrl.hash;}
                        this.page=Math.min(this.page,this.pages);
                        this.uncertain=false;this.persist();confirmed=true;
                        if(manual)this.notice='Текущее состояние прочитано. Проверьте сохранённые значения и конфликты перед новым действием.';
                    }catch(error){if(alive && revision===requestRevision){this.error=error.name==='AbortError'?'Проверка состояния заняла слишком много времени. Повторите чтение.':error.message;}}
                    finally{clearTimeout(timeout);if(alive && revision===requestRevision)this.busy=false;}
                    return confirmed;
                },
                openBulk() {
                    if(this.locked || !this.selected.length)return;
                    if(this.picker.kind==='bulkDictionary') {
                        this.cancelSearch();this.picker={kind:'',rowId:null,fieldId:null,query:'',items:[],loading:false,error:'',more:false};
                    }
                    this.bulkFieldQuery='';this.bulkPlan=null;this.bulkValue='';this.bulkValues=[];this.bulkMode='value';
                    this.syncBulkField();this.bulkSource=this.bulkSources[0]?.draft_id || null;this.error='';this.open('bulk');
                },
                openSourceRefresh() {
                    if(this.locked || !(sourcePrepare || draftSelection))return false;
                    const targets=this.sourceRefreshTargets;
                    if(!targets.length)return false;
                    this.error='';this.sourceRefreshResult=null;
                    this.sourceRefreshReview=targets.map(row=>({
                        draft_id:row.draft_id,
                        draft_version:row.draft_version,
                        imported_product_id:row.imported_product_id,
                        title:row.title,
                    }));
                    this.open('source_refresh');
                    return true;
                },
                async focusSourceRefreshReceipt(expectedFocus) {
                    if(!alive || this.uncertain || !this.sourceRefreshResult)return false;
                    await new Promise(resolve=>this.$nextTick(resolve));
                    const receipt=this.$refs.sourceRefreshReceipt;
                    const currentFocus=global.document.activeElement;
                    if(!alive || this.uncertain || !receipt || receipt.isConnected===false ||
                        currentFocus!==expectedFocus && currentFocus!==global.document.body)return false;
                    receipt.focus({preventScroll:true});
                    receipt.scrollIntoView({block:'start',behavior:'instant'});
                    return true;
                },
                async confirmSourceRefresh() {
                    if(this.locked || this.dialog!=='source_refresh' || !this.sourceRefreshReview.length)return;
                    const targets=this.sourceRefreshReview.map(item=>({...item}));
                    const ids=targets.map(item=>item.draft_id);
                    const byId=new Map(this.rows.map(row=>[row.draft_id,row]));
                    if(new Set(ids).size!==ids.length || targets.some(item=>{
                        const row=byId.get(item.draft_id);
                        return !row || !row.selected || row.source_refresh_available!==true ||
                            row.server || row.unavailable || row.draft_version!==item.draft_version ||
                            row.imported_product_id!==item.imported_product_id;
                    })) {
                        this.error='Выбор или состояние черновиков изменилось. Обновите список и проверьте его снова.';
                        return;
                    }
                    const body=new URLSearchParams();
                    body.append('csrf_token',config.csrf);
                    body.append('refresh_source','1');
                    body.append('confirm_local','1');
                    for(const item of targets) {
                        body.append('selected_draft_id',String(item.draft_id));
                        body.append(`row_${item.draft_id}_draft_version`,String(item.draft_version));
                        body.append(`row_${item.draft_id}_imported_product_id`,String(item.imported_product_id));
                    }
                    this.busy=true;this.error='';this.notice='';this.report=null;this.sourceRefreshResult=null;
                    this.uncertain=true;this.persist();
                    const control=new AbortController(),timeout=setTimeout(()=>control.abort(),45000);
                    let accepted=null,focusAfterClose=null;
                    try {
                        const data=await this.request(this.urls.apply,{method:'POST',body,signal:control.signal});
                        const report=data.report;
                        const reportIds=Array.isArray(report?.rows) ? report.rows.map(row=>row?.draft_id) : [];
                        const reportRowsValid=Array.isArray(report?.rows) && report.rows.length===ids.length &&
                            new Set(reportIds).size===ids.length && reportIds.every(id=>ids.includes(id)) &&
                            report.rows.every(row=>row && ['failed','needs_input','ready_to_retry'].includes(row.status) &&
                                (row.status==='failed' || Number.isSafeInteger(row.version) && row.version>0) &&
                                (row.version===undefined || Number.isSafeInteger(row.version) && row.version>0));
                        const rowCount=status=>Array.isArray(report?.rows)
                            ? report.rows.filter(row=>row?.status===status).length : -1;
                        const countersValid=['updated','ready_to_retry','needs_input','failed','excluded'].every(key=>
                            Number.isSafeInteger(report?.[key]) && report[key]>=0) &&
                            report.updated<=ids.length &&
                            report.ready_to_retry===rowCount('ready_to_retry') &&
                            report.needs_input===rowCount('needs_input') &&
                            report.failed===rowCount('failed') && report.excluded===0 &&
                            report.ready_to_retry+report.needs_input+report.failed===ids.length;
                        const expectedMode=sourcePrepare?'source_prepare':'draft_selection';
                        if(report?.source_refresh!==true || report.mode!==expectedMode ||
                            report.total!==ids.length || !reportRowsValid || !countersValid)
                            throw new Error('Ответ не подтвердил точный набор обновлённых исходных сведений. Сначала сверьте состояние.');
                        if(draftSelection && (data.mode!=='draft_selection' || data.scope_key!==config.editor.scope_key ||
                            report.scope_key!==config.editor.scope_key))
                            throw new Error('Ответ обновления не совпал с выбранным набором. Сначала сверьте состояние.');
                        accepted=report;
                        this.sourceRefreshResult=report;
                    } catch(error) {
                        if(alive){
                            this.error=error.name==='AbortError'?'Ответ не получен вовремя. Сначала сверьте состояние.':error.message;
                            if([401,403].includes(error.status)){this.uncertain=false;this.persist();}
                        }
                    } finally {
                        clearTimeout(timeout);
                        if(alive){
                            this.busy=false;
                            this.close({restoreFocus:!accepted});
                            if(accepted)focusAfterClose=global.document.activeElement;
                        }
                    }
                    if(accepted && alive){
                        // A successful refresh must go through the ordinary version-conflict merge.
                        // Returning a refreshed success row directly would discard pending local edits.
                        const failureRows=accepted.rows.filter(row=>row.status==='failed');
                        const confirmed=await this.reload({report:{rows:failureRows}});
                        if(confirmed && !this.uncertain && global.document.activeElement===focusAfterClose)
                            await this.focusSourceRefreshReceipt(focusAfterClose);
                    }
                },
                resetBulk() {
                    if(this.picker.kind==='bulkDictionary') {
                        this.cancelSearch();this.picker={kind:'',rowId:null,fieldId:null,query:'',items:[],loading:false,error:'',more:false};
                    }
                    this.bulkPlan=null;this.bulkValue='';this.bulkValues=[];this.error='';
                    if(!this.bulkSources.some(row=>row.draft_id===Number(this.bulkSource)))
                        this.bulkSource=this.bulkSources[0]?.draft_id || null;
                },
                previewBulk() {
                    this.bulkPlan=null;
                    if(!this.bulkFixedFields.some(field=>field.key===this.bulkKey) &&
                        !this.bulkAttributeChoices.some(option=>option.key===this.bulkKey)) {
                        this.syncBulkField();this.error='Это поле больше не доступно в выбранных карточках. Выберите его заново.';return;
                    }
                    let value=this.bulkKey.startsWith('attribute:') ? this.bulkValues : this.bulkKey==='schema_cleanup' ? this.bulkValue==='true' : this.bulkValue;
                    let sourceField=this.bulkField;
                    let sourceRow=this.bulkFieldSourceRow;
                    if(this.bulkMode==='copy') {
                        sourceRow=this.bulkSources.find(r=>r.draft_id===Number(this.bulkSource));
                        if(!sourceRow){this.error='Выберите карточку, в которой это поле доступно.';return;}
                        sourceField=sourceRow?.attributes.find(f=>'attribute:'+f.external_id===this.bulkKey);
                        value=sourceField ? sourceField.values : sourceRow?.[this.bulkKey];
                    }
                    if(value==null || value==='' || Array.isArray(value) && (!value.length || value.some(v=>v.value===''))) {this.error='Сначала задайте значение или выберите заполненную строку.';return;}
                    if(this.bulkKey==='schema_cleanup' && this.bulkMode!=='copy' && !['true','false'].includes(this.bulkValue)){this.error='Выберите действие для очистки.';return;}
                    const sourceTypeId=sourceRow && sourceRow.product_type_id===sourceRow.base.product_type_id
                        ? sourceRow.base.product_type_id : null;
                    this.error='';this.bulkPlan=planBulk(this.selected,this.bulkKey,value,sourceField,sourceTypeId);
                },
                applyBulk() {
                    if(!this.bulkPlan || this.locked)return;
                    for(const item of this.bulkPlan.items) {
                        const row=this.rows.find(r=>r.draft_id===item.id),field=row.attributes.find(f=>'attribute:'+f.external_id===this.bulkPlan.key);
                        if(field){this.clearSourceDecision(row,field.external_id);field.values=clone(item.after);if(field.dictionary)row.dictionaryCopied=true;}
                        else {
                            row[this.bulkPlan.key]=clone(item.after);
                            if(['package_width_mm','package_height_mm','package_depth_mm','package_weight_g'].includes(this.bulkPlan.key))
                                this.packageDimensionsChanged(row);
                        }
                        row.error='';
                    }
                    this.notice='Заполнено в форме: '+this.bulkPlan.items.length+'. Изменения ещё не сохранены.';this.close();
                },
                openDictionary(row,field,bulk=false) {
                    if(this.locked || !field.dictionary_fresh)return;
                    this.picker={kind:bulk?'bulkDictionary':'dictionary',rowId:row.draft_id,fieldId:field.external_id,query:'',items:[],loading:false,error:'',more:false};
                    if(!bulk)this.open('picker');this.search();
                },
                openBulkDictionary() {
                    const row=this.bulkFieldSourceRow;
                    if(row && this.bulkField)this.openDictionary(row,this.bulkField,true);
                },
                openType(rows) {
                    if(this.locked)return;this.typeIds=rows.filter(r=>!r.server && !r.unavailable).map(r=>r.draft_id);
                    this.typeChoice=null;this.rememberType=false;
                    this.picker={kind:'type',rowId:null,fieldId:null,query:'',items:[],loading:false,error:'',more:false};
                    this.open('picker');
                },
                search() {
                    clearTimeout(searchTimer);searchController?.abort();const revision=++searchRevision;this.picker.items=[];this.picker.error='';this.picker.loading=true;
                    searchTimer=setTimeout(async()=>{
                        searchController=new AbortController();const control=searchController;const timeout=setTimeout(()=>control.abort(),12000);
                        try{
                            const path=this.picker.kind==='type'
                                ? scopedUrl(config.urls.types,this.picker.query)
                                : scopedUrl(config.urls.dictionaries+this.picker.rowId+'/'+this.picker.fieldId,
                                    this.picker.query,config.urls.dictionary_query);
                            const data=await this.request(path,{signal:control.signal});
                            if(!alive || revision!==searchRevision)return;
                            if(!Array.isArray(data.items))throw new Error('Не удалось прочитать список значений.');
                            this.picker.items=data.items;this.picker.more=!!data.truncated || this.picker.kind==='type' && data.items.length>=30;
                        }catch(error){if(alive && revision===searchRevision)this.picker.error=error.name==='AbortError'?'Поиск не завершился. Повторите запрос.':error.message;}
                        finally{clearTimeout(timeout);if(alive && revision===searchRevision)this.picker.loading=false;}
                    },250);
                },
                choose(item) {
                    if(this.picker.kind==='type'){this.typeChoice=item;this.cancelSearch();return;}
                    const row=this.rows.find(r=>r.draft_id===this.picker.rowId),field=row?.attributes.find(f=>f.external_id===this.picker.fieldId);
                    if(!field)return;
                    const target=this.picker.kind==='bulkDictionary' ? this.bulkValues : field.values;
                    const next=field.is_collection ? [...target.filter(v=>v.value!==item.value),{value:item.value,dictionary_value_id:item.id}] : [{value:item.value,dictionary_value_id:item.id}];
                    if(next.length>field.max_values){this.picker.error='Достигнуто допустимое число значений.';return;}
                    if(this.picker.kind==='bulkDictionary'){this.bulkValues=next;this.cancelSearch();this.picker.kind='';}
                    else{this.clearSourceDecision(row,field.external_id);field.values=next;row.error='';this.close();}
                },
                applyType() {
                    if(!this.typeChoice || this.locked)return;
                    for(const row of this.rows.filter(r=>this.typeIds.includes(r.draft_id))) {
                        for(const id of SOURCE_REVIEW_FIELDS)this.clearSourceDecision(row,id);
                        row.product_type_id=this.typeChoice.id;row.product_type_name=this.typeChoice.name;row.save_mapping=draftSelection ? false : this.rememberType;row.schema_cleanup=false;row.error='';
                    }
                    this.notice='Новый тип выбран в форме. Сохраните его, затем заполните требования новой категории.';this.close();
                },
                suggest(row,field,item) {if(!this.locked && !this.changedType(row)){this.clearSourceDecision(row,field.external_id);field.values=[{value:item.value,dictionary_value_id:item.dictionary_value_id}];}},
                checkRows() {
                    for(const row of this.selected) {
                        if(row.server || row.unavailable)return 'Сначала разрешите конфликты выбранных карточек.';
                        if(row.action==='ИСКЛЮЧИТЬ')continue;
                        if(row.price_rub && !/^\d+([.,]\d+)?$/.test(row.price_rub.trim()))return 'Цена продавца должна быть числом. Проверьте '+row.title+'.';
                        const dimensions=fixed.slice(1,5).map(key=>row[key]);
                        if(dimensions.some(Boolean) && dimensions.some(v=>!v || !/^\d+([.,]\d+)?$/.test(v.trim())))return 'Укажите все четыре значения упаковки числами: '+row.title+'.';
                        if(row.confirm_package===true && !this.packageConfirmationComplete(row))return 'Для подтверждения укажите четыре положительных целых значения упаковки в допустимом диапазоне мм и г: '+row.title+'.';
                    }
                    return '';
                },
                reviewSave() {
                    if(this.locked || !this.selected.length)return;
                    this.error=this.checkRows();if(this.error){this.$nextTick(()=>this.$refs.error?.focus());return;}
                    if(new TextEncoder().encode(serialize(this.selected,config.csrf,this.sourcePrepare,this.draftSelection).toString()).length>2*1024*1024){this.error='Данные выбранных карточек больше 2 МБ. Сохраните меньшую группу.';return;}
                    this.review=this.selected.map(r=>({id:r.draft_id,title:r.title,action:this.sourcePrepare || this.draftSelection?'ИСПРАВИТЬ':r.action,typeChanged:this.changedType(r),impact:r.type_change_impact,changes:changes(r)}));
                    this.open('review');
                },
                async focusReceipt(expectedFocus) {
                    if(!alive || this.uncertain || !this.report)return false;
                    await new Promise(resolve=>this.$nextTick(resolve));
                    const receipt=this.$refs.receipt;
                    if(!alive || this.uncertain || !receipt || receipt.isConnected===false ||
                        global.document.activeElement!==expectedFocus)return false;
                    receipt.focus({preventScroll:true});
                    receipt.scrollIntoView({block:'start',behavior:'instant'});
                    return true;
                },
                async save() {
                    if(this.locked || !this.review.length)return;
                    if(!this.sourceDecisionsResolved){this.error='Для каждого отличающегося предложения выберите действие в сверке с источником.';return;}
                    const ids=this.review.map(r=>r.id),rows=this.rows.filter(r=>ids.includes(r.draft_id));
                    const sourcePrepare=this.sourcePrepare,draftSelection=this.draftSelection;
                    const focusLocalReceipt=sourcePrepare || draftSelection;
                    let accepted=null,focusAfterClose=null;
                    this.busy=true;this.error='';this.report=null;this.sourceRefreshResult=null;
                    const control=new AbortController(),timeout=setTimeout(()=>control.abort(),45000);
                    try{
                        const sourceReviewJSON=sourcePrepare || draftSelection ? await this.sourceReviewJSON(rows) : {};
                        const body=serialize(rows,config.csrf,sourcePrepare,draftSelection,sourceReviewJSON);
                        if(new TextEncoder().encode(body.toString()).length>2*1024*1024)
                            throw new Error('Данные выбранных карточек больше 2 МБ. Сохраните меньшую группу.');
                        this.uncertain=true;this.persist();
                        const data=await this.request(config.urls.apply,{method:'POST',body,signal:control.signal});
                        const report=data.report;
                        if(draftSelection && (data.mode!=='draft_selection' || data.scope_key!==config.editor.scope_key ||
                            report?.mode!=='draft_selection' || report?.scope_key!==config.editor.scope_key))
                            throw new Error('Ответ сохранения не совпал с выбранным набором. Сначала сверьте состояние.');
                        const localOnly=sourcePrepare || draftSelection;
                        if(!report || report.total!==ids.length || !Array.isArray(report.rows) || report.rows.length!==ids.length ||
                            new Set(report.rows.map(r=>r.draft_id)).size!==ids.length || report.rows.some(r=>!ids.includes(r.draft_id) ||
                            !(localOnly ? ['failed','needs_input','ready_to_retry'] : ['failed','excluded','needs_input','ready_to_retry']).includes(r.status)) ||
                            localOnly && report.rows.some(r=>r.status==='excluded'))throw new Error('Результат сохранения неполон. Нужна сверка состояния.');
                        accepted=report;this.report=report;this.notice=sourcePrepare || draftSelection ? '' : 'Сохранение завершено. Готовность каждой карточки показана отдельно.';
                    }catch(error){
                        if(alive){this.error=error.name==='AbortError'?'Ответ не получен вовремя. Сначала сверьте состояние.':error.message;
                            if([401,403].includes(error.status)){this.uncertain=false;this.persist();}}
                    }finally{
                        clearTimeout(timeout);
                        if(alive){
                            this.busy=false;
                            this.close({restoreFocus:!accepted || !focusLocalReceipt});
                            if(accepted && focusLocalReceipt)focusAfterClose=global.document.activeElement;
                        }
                    }
                    if(accepted && alive){
                        const confirmed=await this.reload({report:accepted});
                        if(focusLocalReceipt && confirmed && !this.uncertain && global.document.activeElement===focusAfterClose)
                            await this.focusReceipt(focusAfterClose);
                    }
                },
                openConflict(row) {this.conflictId=row.draft_id;this.open('conflict');},
                canKeep(row) {return row.server && row.base.product_type_id===row.server.base.product_type_id &&
                    equal(row.attributes.map(f=>[f.external_id,shape(f)]),row.server.attributes.map(f=>[f.external_id,shape(f)]));},
                resolveConflict(keep) {
                    const row=this.conflictRow;if(!row?.server || keep && !this.canKeep(row))return;
                    const server=row.server;server.selected=row.selected;server.expanded=true;
                    if(keep)for(const change of changes(row)){
                        if(change.key.startsWith('attribute:'))server.attributes.find(f=>'attribute:'+f.external_id===change.key).values=clone(change.after);
                        else server[change.key]=clone(change.after);
                    }
                    this.rows.splice(this.rows.indexOf(row),1,server);this.close();
                    this.notice=keep?'Ваши правки перенесены на текущую версию. Проверьте их перед сохранением.':'Приняты текущие сохранённые значения.';
                },
            },
            mounted() {
                this.fromURL();
                try{const saved=JSON.parse(global.sessionStorage.getItem(storageKey)||'null');if(saved){this.uncertain=!!saved.uncertain;this.rows.forEach(r=>{r.selected=Array.isArray(saved.selected) && saved.selected.includes(r.draft_id);});}}catch(_){}
                unload=event=>{if(this.dirty || this.busy || this.uncertain){event.preventDefault();event.returnValue='';}};
                pop=()=>this.fromURL();global.addEventListener('beforeunload',unload);global.addEventListener('popstate',pop);
                global.document.getElementById('orb-fallback')?.remove();
            },
            beforeUnmount(){alive=false;controller?.abort();this.cancelSearch();global.removeEventListener('beforeunload',unload);global.removeEventListener('popstate',pop);},
        };
    }
    global.ozonBulkRepair={makeRows,snapshot,changes,definition,serialize,planBulk,validEditor,encodeCompactDraftIds,createOptions,
        safeWorkspaceReturn,workspaceReturnFromFragment};
    const bootstrap=global.document?.getElementById('orb-bootstrap');
    function safeWorkspaceReturn(value) {
        return global.OzonWorkspaceReturn?.safeWorkspaceReturn?.(value, global.location.origin) || null;
    }
    function workspaceReturnFromFragment() {
        return global.OzonWorkspaceReturn?.repairWorkspaceReturnFromHref?.(global.location.href) || null;
    }
    if(bootstrap) {
        try {
            const config=JSON.parse(bootstrap.textContent);
            const workspaceReturn=workspaceReturnFromFragment();
            if(workspaceReturn && config.editor?.mode==='draft_selection' && config.urls) {
                config.urls.catalog_return=workspaceReturn;
                const reviewFragment=global.OzonWorkspaceReturn?.encodeWorkspaceReturnFragment?.(
                    workspaceReturn, global.location.origin);
                if(reviewFragment && config.urls.review) {
                    const reviewUrl=new URL(config.urls.review,global.location.origin);
                    if(reviewUrl.origin===global.location.origin) {
                        reviewUrl.hash=reviewFragment.slice(1);
                        config.urls.review=reviewUrl.pathname+reviewUrl.search+reviewUrl.hash;
                    }
                }
                const fallback=global.document.querySelector('[data-workspace-return-fallback]');
                if(fallback)fallback.href=workspaceReturn;
            }
            bootstrap.textContent=JSON.stringify(config);
            if(global.Vue)global.Vue.createApp(createOptions(config)).mount('#ozon-bulk-repair');
        } catch(_) { /* Keep the server-rendered safe fallback if bootstrap is invalid. */ }
    }
})(window);
