/* Local repair only. Every save keeps the server's exact scope/version/form gate. */
(function (global) {
    'use strict';
    const clone = value => JSON.parse(JSON.stringify(value));
    const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const fixed = ['price_rub','package_width_mm','package_height_mm','package_depth_mm','package_weight_g','description'];
    const labels = {price_rub:'Цена продавца, ₽',package_width_mm:'Ширина упаковки, мм',package_height_mm:'Высота упаковки, мм',package_depth_mm:'Длина упаковки, мм',package_weight_g:'Вес с упаковкой, г',description:'Описание',action:'Действие',product_type_id:'Тип товара',save_mapping:'Запомнить категорию',schema_cleanup:'Очистка несовместимых характеристик'};
    const asText = value => value == null ? '' : String(value);
    const valuesOf = field => asText(field.value).split('\n').filter(Boolean).map(value => ({value, ...(field.dictionary_value_id ? {dictionary_value_id:field.dictionary_value_id} : {})}));
    const shape = field => JSON.stringify([asText(field.data_type).toLowerCase(),!!field.dictionary,!!field.is_collection,field.max_values || 1]);
    function snapshot(row) {
        const result = Object.fromEntries(fixed.map(key => [key, asText(row[key])]));
        for (const field of row.attributes) result['attribute:'+field.external_id] = clone(field.values);
        return {...result,action:row.action,product_type_id:row.product_type_id,save_mapping:!!row.save_mapping,schema_cleanup:!!row.schema_cleanup};
    }
    function makeRows(editor) {
        return editor.groups.flatMap(group => group.rows.map(source => {
            const row = {...clone(source),group_id:group.id,group_name:group.source_category,selected:false,
                expanded:false,save_mapping:false,error:'',server:null,dictionaryCopied:false,baseTypeName:source.product_type_name};
            row.attributes = row.attributes.map(field => ({...field,values:valuesOf(field)}));
            fixed.forEach(key => { row[key] = asText(row[key]); });
            row.base = snapshot(row);
            return row;
        }));
    }
    function changes(row) {
        const current = snapshot(row);
        return Object.keys(current).filter(key => !equal(current[key], row.base[key])).map(key => ({
            key, label:labels[key] || row.attributes.find(f => 'attribute:'+f.external_id===key)?.name || 'Характеристика',
            before:row.base[key], after:current[key],
            beforeLabel:key==='product_type_id' ? row.baseTypeName || 'Не выбран' : display(row.base[key]),
            afterLabel:key==='product_type_id' ? row.product_type_name || 'Не выбран' : display(current[key]),
        }));
    }
    function definition(field) {
        return {...field,id:field.external_id,required:true,collection:field.is_collection,max_values:field.max_values || 1};
    }
    function display(value) {
        if (Array.isArray(value)) return value.map(v => asText(v.value)).join(', ') || 'Не заполнено';
        if (value === true) return 'Да';
        if (value === false) return 'Нет';
        if (value === 'ИСКЛЮЧИТЬ') return 'Исключить из повтора';
        if (value === 'ИСПРАВИТЬ') return 'Исправить';
        return asText(value) || 'Не заполнено';
    }
    function serialize(rows, csrf) {
        const form = new URLSearchParams();
        form.append('csrf_token',csrf);
        for (const row of rows) {
            const prefix = 'row_'+row.draft_id+'_';
            form.append('selected_draft_id',String(row.draft_id));
            for (const [key,value] of Object.entries({draft_version:row.draft_version,imported_product_id:row.imported_product_id,
                action:row.action,product_type_id:row.product_type_id || '',save_mapping:row.save_mapping ? '1':'0'})) form.append(prefix+key,asText(value));
            fixed.forEach(key => form.append(prefix+key,asText(row[key])));
            const typeChanged = row.product_type_id !== row.base.product_type_id;
            if (row.cleanup_candidates.length && row.schema_cleanup && !typeChanged) form.append(prefix+'schema_cleanup','1');
            for (const field of row.attributes.filter(f => f.editable)) {
                const values = typeChanged ? [] : field.values;
                form.append(prefix+'attribute_'+field.external_id,values.map(v => v.value).join('\n'));
                form.append(prefix+'attribute_value_id_'+field.external_id,values.length === 1 ? values[0].dictionary_value_id || '' : '');
            }
        }
        return form;
    }
    function planBulk(rows, key, value, sourceField) {
        const items = [], skipped = [];
        for (const row of rows) {
            let reason = row.action === 'ИСКЛЮЧИТЬ' ? 'Карточка исключается из повтора' : row.server ? 'Сначала разрешите конфликт' : '';
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
            const next = Array.isArray(value) ? value.map(v => ({value:v.value})) : value;
            const before = field ? field.values : row[key];
            if (equal(before,next) || field?.dictionary && equal(before.map(v=>v.value),next.map(v=>v.value))) { skipped.push({id:row.draft_id,title:row.title,reason:'Значение уже совпадает'}); continue; }
            items.push({id:row.draft_id,title:row.title,before:clone(before),after:clone(next)});
        }
        return {key,items,skipped};
    }
    function validEditor(value, config) {
        return value && value.account_id===config.editor.account_id && value.job_uid===config.editor.job_uid &&
            Array.isArray(value.groups) && value.groups.every(g => Array.isArray(g.rows)) &&
            value.groups.reduce((n,g) => n+g.rows.length,0)<=200 &&
            value.groups.flatMap(g => g.rows).every(r => Number.isSafeInteger(r.draft_id) && r.draft_id>0 &&
                Number.isSafeInteger(r.draft_version) && r.draft_version>0 && Array.isArray(r.attributes));
    }
    function createOptions(config) {
        let alive=true, controller=null, searchController=null, searchTimer=null, searchRevision=0, requestRevision=0, unload=null, pop=null, trigger=null;
        const storageKey='ozon-repair:'+config.editor.account_id+':'+config.editor.job_uid;
        return {
            components:{'ozon-field':global.ozonDraftEditor.fieldComponent,'ozon-photo':global.ozonDraftEditor.photoComponent},
            data() { return {rows:makeRows(config.editor),enabled:config.enabled,urls:config.urls,busy:false,error:'',notice:'',
                report:null,uncertain:false,sessionExpired:false,query:'',group:'',state:'',page:1,pageSize:15,dialog:'',
                bulkKey:config.editor.bulk_fields[0]?.key || 'description',bulkValue:'',bulkValues:[],bulkMode:'value',bulkSource:null,bulkPlan:null,
                fields:config.editor.bulk_fields,picker:{kind:'',rowId:null,fieldId:null,query:'',items:[],loading:false,error:'',more:false},
                typeChoice:null,typeIds:[],rememberType:false,review:[],conflictId:null}; },
            computed:{
                selected() { return this.rows.filter(r => r.selected); },
                groups() { return [...new Map(this.rows.map(r => [r.group_id,{id:r.group_id,name:r.group_name}])).values()]; },
                filtered() {
                    const term=this.query.trim().normalize('NFKC').toLocaleLowerCase('ru-RU');
                    return this.rows.filter(r => (!this.group || r.group_id===this.group) &&
                        (!this.state || (this.state==='changed' ? changes(r).length : this.state==='error' ? r.error || r.repair_error || r.server : r.status===this.state)) &&
                        (!term || (r.title+' '+r.offer_id).normalize('NFKC').toLocaleLowerCase('ru-RU').includes(term)));
                },
                pages() { return Math.max(1,Math.ceil(this.filtered.length/this.pageSize)); },
                visible() { return this.filtered.slice((this.page-1)*this.pageSize,this.page*this.pageSize); },
                pageSelected() { return this.visible.length>0 && this.visible.every(r => r.selected); },
                dirty() { return this.rows.some(r => changes(r).length); },
                locked() { return this.busy || this.uncertain || this.sessionExpired || !this.enabled; },
                bulkField() { return this.selected.flatMap(r => r.attributes).find(f => 'attribute:'+f.external_id===this.bulkKey && f.editable) || null; },
                conflictRow() { return this.rows.find(r => r.draft_id===this.conflictId); },
                loginUrl() { return '/login?next='+encodeURIComponent(global.location.pathname+global.location.search); },
            },
            methods:{
                display,definition,changes,
                status(row) { return row.server ? 'Конфликт изменений' : row.error || row.repair_error ? 'Не сохранено' :
                    row.action==='ИСКЛЮЧИТЬ' ? 'Исключена из повтора' : changes(row).length ? 'Есть изменения' :
                    row.status==='ready_to_retry' ? 'Проверена локально' : 'Нужно исправить'; },
                changedType(row) { return row.product_type_id!==row.base.product_type_id; },
                changeField(row,field,values) { field.values=clone(values);row.error=''; },
                noteChange(row) { row.error=''; },
                setFilters() { this.page=1;this.syncURL(); },
                syncURL() {
                    const url=new URL(global.location.href);
                    for (const [key,value] of Object.entries({q:this.query,group:this.group,state:this.state,page:this.page>1 ? this.page : ''})) {
                        if (value) url.searchParams.set(key,String(value));else url.searchParams.delete(key);
                    }
                    global.history.replaceState(null,'',url.pathname+url.search);
                },
                fromURL() { const args=new URL(global.location.href).searchParams;this.query=(args.get('q') || '').slice(0,200);
                    this.group=args.get('group') || '';this.state=args.get('state') || '';this.page=Math.max(1,Math.min(this.pages,Number(args.get('page')) || 1)); },
                goPage(page) { this.page=Math.max(1,Math.min(page,this.pages));this.syncURL(); },
                persist() { try { global.sessionStorage.setItem(storageKey,JSON.stringify({selected:this.selected.map(r=>r.draft_id),uncertain:this.uncertain})); } catch (_) {} },
                selectPage(event) { this.visible.forEach(r => {r.selected=event.target.checked;});this.persist(); },
                selectFiltered() { this.filtered.filter(r=>!r.unavailable).forEach(r=>{r.selected=true;});this.persist(); },
                clearSelection() { this.rows.forEach(r => {r.selected=false;});this.persist(); },
                selectRow(row) { row.selected=!row.selected;this.persist(); },
                open(name) { trigger=global.document.activeElement;this.dialog=name;this.$nextTick(()=>this.$refs.modal.showModal()); },
                close() { if(this.busy)return;this.cancelSearch();this.dialog='';this.$refs.modal.close();trigger?.focus(); },
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
                    if(this.busy && manual)return;
                    this.busy=true;this.error='';const revision=++requestRevision;controller?.abort();controller=new AbortController();
                    const readControl=controller,timeout=setTimeout(()=>readControl.abort(),30000);
                    try {
                        const data=await this.request(config.urls.read,{signal:controller.signal});
                        if(!alive || revision!==requestRevision)return;
                        if(!validEditor(data.editor,config))throw new Error('Не удалось подтвердить магазин и состав загрузки.');
                        if(typeof data.csrf!=='string' || !data.csrf || data.csrf.length>512)throw new Error('Не удалось обновить защиту формы. Повторите чтение состояния.');
                        config.csrf=data.csrf;
                        const fresh=makeRows(data.editor),old=new Map(this.rows.map(r=>[r.draft_id,r]));
                        const outcomes=new Map((report?.rows || []).map(r=>[r.draft_id,r]));
                        this.rows=fresh.map(server=>{
                            const row=old.get(server.draft_id);if(!row)return server;
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
                            row.error='Карточка больше недоступна в этом запуске. Скопируйте ввод перед обновлением.';row.unavailable=true;this.rows.push(row);
                        }
                        this.fields=data.editor.bulk_fields;this.page=Math.min(this.page,this.pages);
                        this.uncertain=false;this.persist();
                        if(manual)this.notice='Текущее состояние прочитано. Проверьте сохранённые значения и конфликты перед новым действием.';
                    }catch(error){if(alive && revision===requestRevision){this.error=error.name==='AbortError'?'Проверка состояния заняла слишком много времени. Повторите чтение.':error.message;}}
                    finally{clearTimeout(timeout);if(alive && revision===requestRevision)this.busy=false;}
                },
                openBulk() { if(this.locked || !this.selected.length)return;this.bulkPlan=null;this.bulkValue='';this.bulkValues=[];this.bulkMode='value';this.bulkSource=this.selected[0].draft_id;this.open('bulk'); },
                resetBulk() {this.bulkPlan=null;this.bulkValue='';this.bulkValues=[];},
                previewBulk() {
                    let value=this.bulkKey.startsWith('attribute:') ? this.bulkValues : this.bulkKey==='schema_cleanup' ? this.bulkValue==='true' : this.bulkValue;
                    let sourceField=this.bulkField;
                    if(this.bulkMode==='copy') {
                        const row=this.selected.find(r=>r.draft_id===Number(this.bulkSource));
                        sourceField=row?.attributes.find(f=>'attribute:'+f.external_id===this.bulkKey);
                        value=sourceField ? sourceField.values : row?.[this.bulkKey];
                    }
                    if(value==null || value==='' || Array.isArray(value) && (!value.length || value.some(v=>v.value===''))) {this.error='Сначала задайте значение или выберите заполненную строку.';return;}
                    if(this.bulkKey==='schema_cleanup' && this.bulkMode!=='copy' && !['true','false'].includes(this.bulkValue)){this.error='Выберите действие для очистки.';return;}
                    this.error='';this.bulkPlan=planBulk(this.selected,this.bulkKey,value,sourceField);
                },
                applyBulk() {
                    if(!this.bulkPlan || this.locked)return;
                    for(const item of this.bulkPlan.items) {
                        const row=this.rows.find(r=>r.draft_id===item.id),field=row.attributes.find(f=>'attribute:'+f.external_id===this.bulkPlan.key);
                        if(field){field.values=clone(item.after);if(field.dictionary)row.dictionaryCopied=true;}
                        else row[this.bulkPlan.key]=clone(item.after);
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
                    const row=this.selected.find(r=>r.attributes.some(f=>'attribute:'+f.external_id===this.bulkKey && f.editable));
                    if(row && this.bulkField)this.openDictionary(row,this.bulkField,true);
                },
                openType(rows) {
                    if(this.locked)return;this.typeIds=rows.filter(r=>!r.server && !r.unavailable).map(r=>r.draft_id);
                    this.typeChoice=null;this.rememberType=false;
                    this.picker={kind:'type',rowId:null,fieldId:null,query:'',items:[],loading:false,error:'',more:false};this.open('picker');
                },
                search() {
                    clearTimeout(searchTimer);searchController?.abort();const revision=++searchRevision;this.picker.items=[];this.picker.error='';this.picker.loading=true;
                    searchTimer=setTimeout(async()=>{
                        searchController=new AbortController();const control=searchController;const timeout=setTimeout(()=>control.abort(),12000);
                        try{
                            const path=this.picker.kind==='type' ? config.urls.types : config.urls.dictionaries+this.picker.rowId+'/'+this.picker.fieldId;
                            const data=await this.request(path+'?q='+encodeURIComponent(this.picker.query),{signal:control.signal});
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
                    else{field.values=next;row.error='';this.close();}
                },
                applyType() {
                    if(!this.typeChoice || this.locked)return;
                    for(const row of this.rows.filter(r=>this.typeIds.includes(r.draft_id))) {
                        row.product_type_id=this.typeChoice.id;row.product_type_name=this.typeChoice.name;row.save_mapping=this.rememberType;row.schema_cleanup=false;row.error='';
                    }
                    this.notice='Новый тип выбран в форме. Сохраните его, затем заполните требования новой категории.';this.close();
                },
                suggest(row,field,item) {if(!this.locked && !this.changedType(row))field.values=[{value:item.value,dictionary_value_id:item.dictionary_value_id}];},
                checkRows() {
                    for(const row of this.selected) {
                        if(row.server || row.unavailable)return 'Сначала разрешите конфликты выбранных карточек.';
                        if(row.action==='ИСКЛЮЧИТЬ')continue;
                        if(row.price_rub && !/^\d+([.,]\d+)?$/.test(row.price_rub.trim()))return 'Цена продавца должна быть числом. Проверьте '+row.title+'.';
                        const dimensions=fixed.slice(1,5).map(key=>row[key]);
                        if(dimensions.some(Boolean) && dimensions.some(v=>!v || !/^\d+([.,]\d+)?$/.test(v.trim())))return 'Укажите все четыре значения упаковки числами: '+row.title+'.';
                    }
                    return '';
                },
                reviewSave() {
                    if(this.locked || !this.selected.length)return;
                    this.error=this.checkRows();if(this.error){this.$nextTick(()=>this.$refs.error?.focus());return;}
                    if(new TextEncoder().encode(serialize(this.selected,config.csrf).toString()).length>2*1024*1024){this.error='Данные выбранных карточек больше 2 МБ. Сохраните меньшую группу.';return;}
                    this.review=this.selected.map(r=>({id:r.draft_id,title:r.title,action:r.action,typeChanged:this.changedType(r),impact:r.type_change_impact,changes:changes(r)}));
                    this.open('review');
                },
                async save() {
                    if(this.locked || !this.review.length)return;
                    const ids=this.review.map(r=>r.id),rows=this.rows.filter(r=>ids.includes(r.draft_id));
                    this.busy=true;this.uncertain=true;this.persist();this.error='';this.report=null;
                    const control=new AbortController(),timeout=setTimeout(()=>control.abort(),45000);
                    let accepted=null;
                    try{
                        const data=await this.request(config.urls.apply,{method:'POST',body:serialize(rows,config.csrf),signal:control.signal});
                        const report=data.report;
                        if(!report || report.total!==ids.length || !Array.isArray(report.rows) || report.rows.length!==ids.length ||
                            new Set(report.rows.map(r=>r.draft_id)).size!==ids.length || report.rows.some(r=>!ids.includes(r.draft_id) ||
                            !['failed','excluded','needs_input','ready_to_retry'].includes(r.status)))throw new Error('Результат сохранения неполон. Нужна сверка состояния.');
                        accepted=report;this.report=report;this.notice='Сохранение завершено. Готовность каждой карточки показана отдельно.';
                    }catch(error){
                        if(alive){this.error=error.name==='AbortError'?'Ответ не получен вовремя. Сначала сверьте состояние.':error.message;
                            if([401,403].includes(error.status)){this.uncertain=false;this.persist();}}
                    }finally{clearTimeout(timeout);if(alive){this.busy=false;this.close();}}
                    if(accepted && alive)await this.reload({report:accepted});
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
    global.ozonBulkRepair={makeRows,snapshot,changes,definition,serialize,planBulk,validEditor,createOptions};
    const bootstrap=global.document?.getElementById('orb-bootstrap');
    if(bootstrap && global.Vue)global.Vue.createApp(createOptions(JSON.parse(bootstrap.textContent))).mount('#ozon-bulk-repair');
})(window);
