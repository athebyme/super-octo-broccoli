/* Completed Ozon accrual snapshots; components never contribute to totals. */
(function(global){
  'use strict';
  const categories={POSTING:'По отправлению',ITEM:'По товару',NON_ITEM:'По продавцу',UNSPECIFIED:'Категория не указана'};
  const positive=(v,fallback=1)=>/^[1-9]\d*$/.test(String(v||'')) && Number(v)<=100000?Number(v):fallback;
  const instant=v=>v?new Date(/[Zz]|[+-]\d\d:\d\d$/.test(v)?v:v+'Z'):null;
  function date(v){const d=instant(v);return d&&Number.isFinite(d.getTime())?d.toLocaleString('ru-RU',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}):'Дата не указана';}
  function day(v){const d=/^\d{4}-\d{2}-\d{2}$/.test(v||'')?new Date(v+'T00:00:00'):null;return d&&Number.isFinite(d.getTime())?d.toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'}):'Дата не указана';}
  function money(value,currency){
    if(!['string','number'].includes(typeof value))return 'Сумма не указана';
    const match=/^(-?)(\d+)(?:\.(\d{1,4}))?$/.exec(String(value??''));if(!match||match[2].length>30)return 'Сумма не указана';
    const whole=BigInt(match[2]);let fraction=match[3]||'';while(fraction.length>2&&fraction.endsWith('0'))fraction=fraction.slice(0,-1);fraction=fraction.padEnd(2,'0');
    const negative=match[1]&&(whole!==0n||/[1-9]/.test(fraction));
    const amount=(negative?'−':'')+new Intl.NumberFormat('ru-RU').format(whole)+','+fraction;
    if(!/^[A-Z]{3}$/.test(currency||''))return amount+' · валюта не указана';
    try{return amount+'\u00a0'+new Intl.NumberFormat('ru-RU',{style:'currency',currency}).formatToParts(0).find(p=>p.type==='currency').value;}catch(_){return amount+'\u00a0'+currency;}
  }
  function pagination(v,page,max){return v&&v.page===page&&Number.isSafeInteger(v.per_page)&&v.per_page>0&&v.per_page<=max&&Number.isSafeInteger(v.total)&&v.total>=0&&v.pages===Math.ceil(v.total/v.per_page);}
  function safeImage(v){try{const u=new URL(v);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:'';}catch(_){return '';}}
  function createOptions(config){
    let alive=true,listController,detailController,exportController,exportRevision=0,listRevision=0,detailRevision=0,pop,returnFocus,previousOverflow='';
    const refresh=global.ozonReadRefresh({accountId:config.accountId,domain:'finance',csrfToken:config.csrfToken,notify:message=>global.Alpine?.store('toasts')?.success(message)});
    const refreshData={},refreshMethods={};for(const [k,v] of Object.entries(refresh))(typeof v==='function'?refreshMethods:refreshData)[k]=v;
    function fromURL(){const q=new URLSearchParams(location.search);return {period:['7d','30d'].includes(q.get('period'))?q.get('period'):'30d',search:(q.get('search')||'').slice(0,200),category:categories[q.get('category')]?q.get('category'):'',sign:['positive','negative','zero'].includes(q.get('sign'))?q.get('sign'):'',typeId:/^[1-9]\d{0,17}$/.test(q.get('type_id')||'')?q.get('type_id'):'',snapshotId:/^[1-9]\d{0,15}$/.test(q.get('snapshot_id')||'')?q.get('snapshot_id'):'',asOf:q.has('snapshot_id')&&/^\d{4}-\d{2}-\d{2}$/.test(q.get('as_of')||'')?q.get('as_of'):'',page:positive(q.get('page'))};}
    async function read(url,controller){let timedOut=false;const timer=setTimeout(()=>{timedOut=true;controller.abort();},10000);try{
      const r=await fetch(url,{signal:controller.signal,credentials:'same-origin',headers:{Accept:'application/json'},cache:'no-store'});
      if(r.redirected||[401,403].includes(r.status))throw Object.assign(Error('Сессия завершилась или доступ изменился. Войдите снова, чтобы продолжить.'),{sessionEnded:true});
      if(!(r.headers.get('content-type')||'').includes('application/json'))throw Error('Не удалось прочитать ответ. Повторите загрузку.');
      const body=await r.json();if(!r.ok||body.success===false)throw Error(body.error||'Не удалось загрузить начисления. Повторите попытку.');
      if(!body.data||typeof body.data!=='object')throw Error('Сервер не вернул данные. Повторите загрузку.');return body.data;
    }catch(e){if(timedOut)throw Error('Загрузка заняла слишком много времени. Повторите попытку.');if(e instanceof TypeError)throw Error('Нет соединения с сервером. Сохранённые данные остаются на экране.');throw e;}finally{clearTimeout(timer);}}
    return {
      directives:{'image-deadline':global.mcatShared.imageDeadline},
      data:()=>({...refreshData,config,categories,period:'30d',filters:fromURL(),searchDraft:'',observedFilters:null,items:[],totals:[],typeCounts:[],typesTruncated:false,pagination:{page:1,pages:0,total:0},snapshot:null,syncState:null,coverage:null,loaded:false,loading:false,error:'',sessionEnded:false,failedImages:{},detailId:null,detail:null,detailLoading:false,detailError:'',exporting:false,exportError:'',exportMessage:'',itemPage:1,componentPage:1}),
      computed:{
        canExport(){return this.loaded&&!this.loading&&!this.staleScope&&!this.error&&!this.sessionEnded&&!!this.filters.snapshotId&&!!this.filters.asOf;},
        hasFilters(){return Boolean(this.filters.search||this.filters.category||this.filters.sign||this.filters.typeId);},
        staleScope(){return this.observedFilters&&JSON.stringify(this.observedFilters)!==JSON.stringify(this.filters);},
        loginUrl(){return '/login?next='+encodeURIComponent(location.pathname+location.search);},
        freshness(){return this.snapshot?.completed_at?'Данные обновлены '+date(this.snapshot.completed_at):'Завершённой загрузки за этот период ещё нет';},
        typeOptions(){const result=new Map();for(const t of this.typeCounts){const id=String(t.external_type_id);if(!result.has(id))result.set(id,{value:id,label:t.name||'Тип '+id});}if(this.filters.typeId&&!result.has(this.filters.typeId))result.set(this.filters.typeId,{value:this.filters.typeId,label:'Тип '+this.filters.typeId});return [...result.values()];},
      },
      methods:{...refreshMethods,date,day,money,safeImage,
        categoryLabel(v){return categories[v]||v||'Категория не указана';},
        matchLabel(v){return {matched:'Карточка связана',unmatched:'Карточка ещё не найдена',ambiguous:'Несколько карточек с этим SKU',unavailable:'Связанная карточка недоступна'}[v]||'Связь не подтверждена';},
        componentLabel(c){return c.type_name||'Тип начисления '+c.external_type_id;},
        lineName(line){return line.title||line.listing?.title||'SKU '+line.external_sku;},
        url(filters=this.filters,factId=null,itemPage=1,componentPage=1){const q=new URLSearchParams({account_id:config.accountId,period:filters.period});for(const key of ['search','category','sign'])if(filters[key])q.set(key,filters[key]);if(filters.typeId)q.set('type_id',filters.typeId);if(filters.snapshotId){q.set('snapshot_id',filters.snapshotId);if(filters.asOf)q.set('as_of',filters.asOf);}if(filters.page>1)q.set('page',filters.page);if(factId){q.set('fact_id',factId);if(itemPage>1)q.set('item_page',itemPage);if(componentPage>1)q.set('component_page',componentPage);}return config.page+'?'+q;},
        async load(){if(!alive||this.sessionEnded)return;listController?.abort();listController=new AbortController();const revision=++listRevision,scope={...this.filters};this.loading=true;this.error='';
          const q=new URLSearchParams({account_id:config.accountId,period:scope.period,page:scope.page,per_page:25,view:'compact'});for(const k of ['search','category','sign'])if(scope[k])q.set(k,scope[k]);if(scope.typeId)q.set('type_id',scope.typeId);if(scope.snapshotId){q.set('snapshot_id',scope.snapshotId);if(scope.asOf)q.set('as_of',scope.asOf);}
          try{const data=await read(config.api+'?'+q,listController);if(!alive||revision!==listRevision)return;
            if(data.scope?.account_id!==config.accountId||!Array.isArray(data.items)||!Array.isArray(data.totals)||!pagination(data.pagination,scope.page,25))throw Error('Не удалось подтвердить начисления выбранного магазина. Повторите загрузку.');
            if(scope.snapshotId && String(data.snapshot_sync?.id)!==scope.snapshotId)throw Error('Получен другой снимок. Откройте актуальные данные.');
            if(data.snapshot_sync){if(!Number.isSafeInteger(data.snapshot_sync.id)||data.snapshot_sync.id<=0)throw Error('Не удалось подтвердить снимок начислений.');scope.snapshotId=String(data.snapshot_sync.id);scope.asOf=data.coverage?.requested_end||scope.asOf;this.filters.snapshotId=scope.snapshotId;this.filters.asOf=scope.asOf;history.replaceState(history.state,'',this.url(this.filters,this.detailId,this.itemPage,this.componentPage));}
            this.items=data.items;this.totals=data.totals;this.pagination=data.pagination;this.typeCounts=data.type_counts||[];this.typesTruncated=!!data.type_counts_truncated;this.snapshot=data.snapshot_sync||null;this.syncState=data.sync||null;this.coverage=data.coverage||null;this.observedFilters=scope;this.loaded=true;
          }catch(e){if(alive&&revision===listRevision&&e.name!=='AbortError'){this.error=e.message;this.sessionEnded=!!e.sessionEnded;}}finally{if(alive&&revision===listRevision)this.loading=false;}
        },
        navigate(patch){this.cancelExport();const oldPeriod=this.period;this.filters={...this.filters,...patch};this.period=this.filters.period;this.searchDraft=this.filters.search;this.dismissDetail(false);history.pushState({},'',this.url());this.load();if(oldPeriod!==this.period)this.changeRefreshPeriod();},
        applyFilters(){this.navigate({search:this.searchDraft.trim(),page:1});},resetFilters(){this.navigate({search:'',category:'',sign:'',typeId:'',page:1});},changePeriod(period){this.navigate({period,page:1,snapshotId:'',asOf:''});},
        pageLink(event,page){if(event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;event.preventDefault();this.navigate({page});},
        async onRefreshCompleted(info){if(info?.fromActive || info?.afterUncertain)this.openLatest();else await this.load();},
        openLatest(){this.navigate({snapshotId:'',page:1});},
        async openDetail(id,push=true,itemPage=1,componentPage=1){if(!alive||this.sessionEnded||!Number.isSafeInteger(id)||id<=0)return;if(this.detailId!==id)this.detail=null;this.detailId=id;this.itemPage=itemPage;this.componentPage=componentPage;this.detailError='';
          if(push)history.pushState({financeDetail:id},'',this.url(this.filters,id,itemPage,componentPage));await this.$nextTick();
          const panel=this.$refs.drawer;if(panel&&!panel.open){returnFocus=document.activeElement;previousOverflow=document.body.style.overflow;panel.showModal();document.body.style.overflow='hidden';}
          detailController?.abort();detailController=new AbortController();const revision=++detailRevision;this.detailLoading=true;
          try{const data=await read(config.api+'/'+id+'?'+new URLSearchParams({account_id:config.accountId,view:'compact',item_page:itemPage,component_page:componentPage,per_page:50}),detailController);if(!alive||revision!==detailRevision)return;
            if(data.id!==id||data.account_id!==config.accountId||(this.filters.snapshotId&&String(data.sync_id)!==this.filters.snapshotId)||!Array.isArray(data.items)||!Array.isArray(data.components)||!pagination(data.items_pagination,itemPage,50)||!pagination(data.components_pagination,componentPage,50))throw Error('Не удалось подтвердить начисление выбранного магазина.');this.detail=data;
          }catch(e){if(alive&&revision===detailRevision&&e.name!=='AbortError'){this.detailError=e.message;this.sessionEnded=!!e.sessionEnded;}}finally{if(alive&&revision===detailRevision)this.detailLoading=false;}
        },
        detailPage(kind,page){const ip=kind==='items'?page:this.itemPage,cp=kind==='components'?page:this.componentPage;history.replaceState(history.state,'',this.url(this.filters,this.detailId,ip,cp));this.openDetail(this.detailId,false,ip,cp);},
        dismissDetail(updateURL=true){const id=this.detailId;detailRevision++;detailController?.abort();this.detailId=null;this.detailLoading=false;if(this.$refs.drawer?.open){this.$refs.drawer.close();document.body.style.overflow=previousOverflow;returnFocus?.isConnected&&returnFocus.focus();}if(updateURL&&id){if(history.state?.financeDetail===id)history.back();else history.replaceState({},'',this.url());}},
        cancelExport(){exportRevision++;exportController?.abort();this.exporting=false;this.exportError='';this.exportMessage='';},
        async exportExcel(){
          if(!alive||!this.canExport||this.exporting)return;
          this.exporting=true;this.exportError='';this.exportMessage='';const revision=++exportRevision,scope={...this.filters};exportController=new AbortController();
          const controller=exportController;let expired=false;const timer=setTimeout(()=>{expired=true;controller.abort();},30000);
          const q=new URLSearchParams({account_id:config.accountId,period:scope.period,snapshot_id:scope.snapshotId,as_of:scope.asOf});
          for(const key of ['search','category','sign'])if(scope[key])q.set(key,scope[key]);if(scope.typeId)q.set('type_id',scope.typeId);
          try{
            const r=await fetch(config.exportApi+'?'+q,{signal:controller.signal,credentials:'same-origin',cache:'no-store',headers:{Accept:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}});
            if(r.redirected||[401,403].includes(r.status))throw Object.assign(Error('Сессия завершилась или доступ изменился. Войдите снова для выгрузки.'),{sessionEnded:true});
            if(!r.ok){let message='Не удалось подготовить файл. Повторите попытку.';try{message=(await r.json()).error||message;}catch(_){}throw Error(message);}
            if(!(r.headers.get('content-type')||'').startsWith('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')||r.headers.get('x-finance-account-id')!==String(config.accountId)||r.headers.get('x-finance-snapshot-id')!==scope.snapshotId||r.headers.get('x-finance-as-of')!==scope.asOf||r.headers.get('x-finance-period')!==scope.period)throw Error('Не удалось подтвердить магазин и период файла. Повторите выгрузку.');
            if(Number(r.headers.get('content-length'))>16*1024*1024)throw Error('Файл слишком большой. Сократите период или уточните фильтры.');
            const blob=await r.blob();if(!blob.size||blob.size>16*1024*1024)throw Error('Не удалось получить полный файл. Повторите выгрузку.');
            if(!alive||revision!==exportRevision||JSON.stringify(scope)!==JSON.stringify(this.filters))return;
            const filename=/filename="?([A-Za-z0-9_.-]+\.xlsx)"?/.exec(r.headers.get('content-disposition')||'')?.[1];if(!filename)throw Error('Сервер не подтвердил имя файла. Повторите выгрузку.');
            const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=filename;link.hidden=true;document.body.appendChild(link);
            try{link.click();this.exportMessage='Файл подготовлен. В нём вся выбранная выборка, товары и расшифровка.';}finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
          }catch(e){if(alive&&revision===exportRevision){if(expired)this.exportError='Подготовка файла заняла слишком много времени. Сократите период или повторите попытку.';else if(e.name!=='AbortError')this.exportError=e instanceof TypeError?'Нет соединения с сервером. Повторите выгрузку.':e.message;if(e.sessionEnded){this.sessionEnded=true;this.refreshSessionEnded=true;this.destroyRefresh();}}}
          finally{clearTimeout(timer);if(alive&&revision===exportRevision)this.exporting=false;}
        },
        retryImages(){this.failedImages={};},
      },
      mounted(){document.getElementById('ofn-fallback')?.remove();this.period=this.filters.period;this.searchDraft=this.filters.search;
        const detailFromURL=()=>{const q=new URLSearchParams(location.search);if(/^[1-9]\d*$/.test(q.get('fact_id')||''))this.openDetail(Number(q.get('fact_id')),false,positive(q.get('item_page')),positive(q.get('component_page')));else this.dismissDetail(false);};
        pop=()=>{const previous=JSON.stringify(this.filters),oldPeriod=this.period;this.filters=fromURL();this.period=this.filters.period;this.searchDraft=this.filters.search;if(previous!==JSON.stringify(this.filters)){this.cancelExport();this.load();}if(oldPeriod!==this.period)this.changeRefreshPeriod();detailFromURL();};window.addEventListener('popstate',pop);this.load();this.initRefresh();detailFromURL();
      },
      beforeUnmount(){this.cancelExport();alive=false;listRevision++;detailRevision++;listController?.abort();detailController?.abort();this.destroyRefresh();window.removeEventListener('popstate',pop);this.dismissDetail(false);},
    };
  }
  global.ozonFinance={createOptions,money,date,day};const bootstrap=document.getElementById('ofn-bootstrap');if(bootstrap&&global.Vue)global.Vue.createApp(createOptions(JSON.parse(bootstrap.textContent))).mount('#ofn-app');
})(window);
