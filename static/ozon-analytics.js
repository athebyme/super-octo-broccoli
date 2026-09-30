/* Observed Ozon analytics: exact decimal labels, one pinned snapshot, local chart. */
(function(global){
  'use strict';
  const REVENUE='ordered_revenue_rub',UNITS='ordered_units',METRICS=[REVENUE,UNITS];
  const positive=(v,fallback=1)=>/^[1-9]\d*$/.test(String(v||''))&&Number(v)<=100000?Number(v):fallback;
  function scaled(v){const m=/^(\d{1,20})(?:\.(\d{1,4}))?$/.exec(typeof v==='string'?v:'');return m?BigInt(m[1])*10000n+BigInt((m[2]||'').padEnd(4,'0')):null;}
  function decimal(v,money=false){
    if(scaled(v)===null)return '—';const [whole,raw='']=v.split('.');let fraction=raw.replace(/0+$/,'');if(money)fraction=fraction.padEnd(2,'0');
    return new Intl.NumberFormat('ru-RU').format(BigInt(whole))+(fraction?','+fraction:'')+(money?'\u00a0₽':'');
  }
  function percent(v,total){const value=scaled(v),base=scaled(total);return value===null||base===null||base===0n||value>base?null:Number((value*10000n+base/2n)/base)/100;}
  function day(v,short=false){const d=/^\d{4}-\d{2}-\d{2}$/.test(v||'')?new Date(v+'T00:00:00'):null;return d&&Number.isFinite(d.getTime())?d.toLocaleDateString('ru-RU',{day:'numeric',month:short?'short':'long',...(short?{}:{year:'numeric'})}):'Дата не указана';}
  function instant(v){const d=v?new Date(/[Zz]|[+-]\d\d:\d\d$/.test(v)?v:v+'Z'):null;return d&&Number.isFinite(d.getTime())?d.toLocaleString('ru-RU',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}):'Дата не указана';}
  function imageURL(v){try{const u=new URL(v);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:'';}catch(_){return '';}}
  function createOptions(config){
    let alive=true,revision=0,controller,pop;
    const refresh=global.ozonReadRefresh({accountId:config.accountId,domain:'analytics',csrfToken:config.csrfToken,notify:message=>global.Alpine?.store('toasts')?.success(message)});
    const refreshData={},refreshMethods={};for(const [key,value] of Object.entries(refresh))(typeof value==='function'?refreshMethods:refreshData)[key]=value;
    function fromURL(){const q=new URLSearchParams(location.search);return {
      filters:{period:['7d','30d'].includes(q.get('period'))?q.get('period'):'30d',search:(q.get('search')||'').slice(0,200),
        sortBy:METRICS.includes(q.get('sort_by'))?q.get('sort_by'):REVENUE,sortDir:q.get('sort_dir')==='asc'?'asc':'desc',page:positive(q.get('page')),
        snapshotId:/^[1-9]\d{0,17}$/.test(q.get('snapshot_id')||'')?q.get('snapshot_id'):'',asOf:q.get('snapshot_id')&&/^\d{4}-\d{2}-\d{2}$/.test(q.get('as_of')||'')?q.get('as_of'):''},
      metric:METRICS.includes(q.get('metric'))?q.get('metric'):REVENUE,selectedDay:q.get('day')||'',showDailyTable:q.get('view')==='days',
    };}
    async function read(url,current){let expired=false;const timer=setTimeout(()=>{expired=true;current.abort();},10000);
      try{const response=await fetch(url,{signal:current.signal,credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}});
        if(response.redirected||[401,403].includes(response.status))throw Object.assign(Error('Сессия завершилась или доступ изменился. Войдите снова, чтобы продолжить.'),{sessionEnded:true});
        if(!(response.headers.get('content-type')||'').includes('application/json'))throw Error('Не удалось прочитать ответ. Повторите загрузку.');
        const body=await response.json();if(!response.ok||body.success===false)throw Error(body.error||'Не удалось загрузить аналитику. Повторите попытку.');return body.data;
      }catch(e){if(expired)throw Error('Загрузка заняла слишком много времени. Повторите попытку.');if(e instanceof TypeError)throw Error('Нет соединения с сервером. Повторите загрузку.');throw e;}finally{clearTimeout(timer);}
    }
    return {
      directives:{'image-deadline':global.mcatShared.imageDeadline},
      data(){const state=fromURL();return {...refreshData,config,REVENUE,UNITS,...state,period:state.filters.period,searchDraft:state.filters.search,
        loaded:false,loading:false,error:'',sessionEnded:false,observedFilters:null,snapshot:null,status:'no_data',totals:{},daily:[],products:[],
        pagination:{page:1,per_page:25,total:0,pages:0},requestedPeriod:null,definitions:[],failedImages:{}};},
      computed:{
        loginUrl(){return '/login?next='+encodeURIComponent(location.pathname+location.search);},
        staleScope(){return this.observedFilters&&JSON.stringify(this.observedFilters)!==JSON.stringify(this.filters);},
        selected(){return this.daily.find(d=>d.date===this.selectedDay)||null;},
        metricLabel(){return this.metric===REVENUE?'Сумма заказанных товаров':'Заказанные единицы';},
        shareLabel(){return this.metric===REVENUE?'Доля суммы':'Доля единиц';},
        periodLabel(){return this.snapshot?day(this.snapshot.period_start)+' — '+day(this.snapshot.period_end):'';},
        chart(){
          const values=this.daily.map(d=>scaled(d[this.metric]));const maximum=values.reduce((m,v)=>v!==null&&v>m?v:m,0n);const width=600/Math.max(this.daily.length,1);
          return {maximum:this.daily.find(d=>scaled(d[this.metric])===maximum)?.[this.metric]||null,
            bars:this.daily.map((d,i)=>{const value=values[i],height=value!==null&&maximum>0n?Number(value*15000n/maximum)/100:0;
              return {date:d.date,x:i*width+3,width:Math.max(1,width-6),y:164-height,height,known:value!==null,zero:value===0n};}),
            known:values.filter(v=>v!==null).length};
        },
      },
      methods:{...refreshMethods,decimal,day,instant,imageURL,
        metricValue(value,metric=this.metric){return decimal(value,metric===REVENUE);},
        share(item){return percent(item.metrics?.[this.metric],this.totals[this.metric]);},
        shareText(item){const value=this.share(item);return value===null?'—':new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(value)+'\u00a0%';},
        url(filters=this.filters){const q=new URLSearchParams({account_id:config.accountId,period:filters.period});
          if(filters.search)q.set('search',filters.search);if(filters.sortBy!==REVENUE)q.set('sort_by',filters.sortBy);if(filters.sortDir!=='desc')q.set('sort_dir',filters.sortDir);if(filters.page>1)q.set('page',filters.page);
          if(filters.snapshotId){q.set('snapshot_id',filters.snapshotId);if(filters.asOf)q.set('as_of',filters.asOf);}if(this.metric!==REVENUE)q.set('metric',this.metric);if(this.selectedDay)q.set('day',this.selectedDay);if(this.showDailyTable)q.set('view','days');return config.page+'?'+q;},
        displayChanged(){history.pushState({},'',this.url());},
        changeMetric(metric){this.metric=metric;this.displayChanged();},
        toggleDays(){this.showDailyTable=!this.showDailyTable;this.displayChanged();},
        navigate(patch){const oldPeriod=this.period;this.filters={...this.filters,...patch};this.period=this.filters.period;this.searchDraft=this.filters.search;
          history.pushState({},'',this.url());this.load();if(oldPeriod!==this.period)this.changeRefreshPeriod();},
        applySearch(){this.navigate({search:this.searchDraft.trim(),page:1});},
        resetSearch(){this.navigate({search:'',page:1});},
        changePeriod(period){this.navigate({period,page:1,snapshotId:'',asOf:''});},
        openLatest(){this.navigate({snapshotId:'',asOf:'',page:1});},
        pageLink(event,page){if(event.metaKey||event.ctrlKey||event.altKey||event.shiftKey)return;event.preventDefault();this.navigate({page});},
        async onRefreshCompleted(info){if(info?.fromActive||info?.afterUncertain)this.openLatest();else if(!this.loaded&&!this.loading)await this.load();},
        async load(){if(!alive||this.sessionEnded)return;controller?.abort();controller=new AbortController();const current=controller,version=++revision,scope={...this.filters};this.loading=true;this.error='';
          const query=new URLSearchParams({account_id:config.accountId,period:scope.period,page:scope.page,per_page:25,sort_by:scope.sortBy,sort_dir:scope.sortDir});if(scope.search)query.set('search',scope.search);if(scope.snapshotId){query.set('snapshot_id',scope.snapshotId);if(scope.asOf)query.set('as_of',scope.asOf);}
          try{const data=await read(config.api+'?'+query,current);if(!alive||version!==revision)return;
            const p=data?.pagination;
            if(data?.scope?.account_id!==config.accountId||data.scope.marketplace_code!=='ozon'||data.scope.cross_marketplace_comparable!==false||!Array.isArray(data.products)||data.products.length>25||!Array.isArray(data.daily)||data.daily.length>31||!p||p.page!==scope.page||p.per_page!==25||!Number.isSafeInteger(p.total)||p.total<0||p.pages!==Math.ceil(p.total/25)||!['ready','stale','no_data'].includes(data.status)||!data.totals)throw Error('Не удалось подтвердить аналитику выбранного магазина. Повторите загрузку.');
            if(data.filters?.period!==scope.period||data.filters.search!==scope.search||data.filters.sort_by!==scope.sortBy||data.filters.sort_dir!==scope.sortDir)throw Error('Получена другая выборка. Повторите загрузку.');
            if(scope.snapshotId&&String(data.snapshot?.id)!==scope.snapshotId)throw Error('Получен другой снимок. Откройте актуальные данные.');
            if(scope.asOf&&data.as_of!==scope.asOf)throw Error('Получены другие даты. Откройте актуальные данные.');
            if(data.snapshot){if(!Number.isSafeInteger(data.snapshot.id)||data.snapshot.id<=0||!/^\d{4}-\d{2}-\d{2}$/.test(data.as_of||''))throw Error('Не удалось подтвердить период аналитики.');scope.snapshotId=String(data.snapshot.id);scope.asOf=data.as_of;}
            this.filters.snapshotId=scope.snapshotId;this.filters.asOf=scope.asOf;this.snapshot=data.snapshot||null;this.status=data.status;this.totals=data.totals;this.daily=data.daily;this.products=data.products;this.pagination=p;this.requestedPeriod=data.requested_period;this.definitions=data.definitions||[];
            if(!this.daily.some(d=>d.date===this.selectedDay))this.selectedDay=this.daily.at(-1)?.date||'';
            this.observedFilters=scope;this.loaded=true;history.replaceState(history.state,'',this.url());
          }catch(e){if(alive&&version===revision&&e.name!=='AbortError'){this.error=e.message;if(e.sessionEnded){this.sessionEnded=true;this.refreshSessionEnded=true;this.destroyRefresh();}}}
          finally{if(alive&&version===revision)this.loading=false;}
        },
        retryImages(){this.failedImages={};},
      },
      mounted(){document.getElementById('oan-fallback')?.remove();this.load();this.initRefresh();
        pop=()=>{const previous=JSON.stringify(this.filters),oldPeriod=this.period,next=fromURL();Object.assign(this,next);this.period=this.filters.period;this.searchDraft=this.filters.search;if(previous!==JSON.stringify(this.filters))this.load();if(oldPeriod!==this.period)this.changeRefreshPeriod();};window.addEventListener('popstate',pop);},
      beforeUnmount(){alive=false;revision++;controller?.abort();window.removeEventListener('popstate',pop);this.destroyRefresh();},
    };
  }
  global.ozonAnalytics={createOptions,decimal,scaled,percent,day};const bootstrap=document.getElementById('oan-bootstrap');if(bootstrap&&global.Vue)global.Vue.createApp(createOptions(JSON.parse(bootstrap.textContent))).mount('#oan-app');
})(window);
