/* Seller-scoped order workspace. All writes here enqueue read-only refreshes. */
(function (global) {
  'use strict';
  const statuses = {
    awaiting_registration:['Ожидает регистрации','warn'], acceptance_in_progress:['Приёмка','info'],
    awaiting_approve:['Ожидает подтверждения','warn'], awaiting_packaging:['Ожидает сборки','warn'],
    awaiting_deliver:['Ожидает отгрузки','warn'], delivering:['Доставляется','info'],
    driver_pickup:['У курьера','info'], delivered:['Доставлено','ok'], cancelled:['Отменено','muted'],
    arbitration:['Разбирается спор','warn'], client_arbitration:['Спор с покупателем','warn'],
    not_accepted:['Не принят','danger'], sent_by_seller:['Передано продавцом','info'],
    MovingToSeller:['Возвращается продавцу','info'], ReceivedBySeller:['Получено продавцом','ok'],
  };
  const sources = {fbo:'FBO',fbs:'FBS',rfbs:'rFBS',fbo_fbs:'FBO и FBS',posting_fbo:'FBO',posting_fbs:'FBS',rfbs_conditional:'Заявка rFBS'};
  const positive = (value, fallback=1) => /^[1-9]\d*$/.test(String(value || '')) && Number(value)<=100000 ? Number(value) : fallback;
  const instant = value => value ? new Date(/[Zz]|[+-]\d\d:\d\d$/.test(value) ? value : value+'Z') : null;
  function date(value) { const d=instant(value); return d && Number.isFinite(d.getTime()) ? d.toLocaleString('ru-RU',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}) : 'Дата не указана'; }
  function money(value, currency) {
    if (value === null || value === undefined || value === '' || !/^\d+(\.\d+)?$/.test(String(value)) || !Number.isFinite(Number(value))) return 'Цена не указана';
    if (!/^[A-Z]{3}$/.test(currency || '')) return Number(value).toLocaleString('ru-RU')+' · валюта не указана';
    try { return new Intl.NumberFormat('ru-RU',{style:'currency',currency,maximumFractionDigits:4}).format(Number(value)); }
    catch (_) { return Number(value).toLocaleString('ru-RU')+' '+currency; }
  }
  function validPagination(value, requestedPage, maximum) { return value && value.page===requestedPage && Number.isSafeInteger(value.per_page) && value.per_page>0 && value.per_page<=maximum && Number.isSafeInteger(value.total) && value.total>=0 && Number.isSafeInteger(value.pages) && value.pages===Math.ceil(value.total/value.per_page); }
  function safeImage(value) { try { const u=new URL(value);return ['http:','https:'].includes(u.protocol) && !u.username && !u.password ? u.href : ''; } catch (_) { return ''; } }
  function createOptions(config) {
    let alive=true, listController, detailController, listVersion=0, detailVersion=0, pop, returnFocus, previousOverflow='';
    const refresh=global.ozonReadRefresh({accountId:config.accountId,domain:'fulfillment',csrfToken:config.csrfToken,
      notify:message=>global.Alpine?.store('toasts')?.success(message)});
    const refreshData={},refreshMethods={};
    for (const [key,value] of Object.entries(refresh)) (typeof value === 'function' ? refreshMethods : refreshData)[key]=value;
    async function read(url, controller) {
      let timedOut=false;const timer=setTimeout(()=>{timedOut=true;controller.abort();},10000);
      try {
        const response=await fetch(url,{signal:controller.signal,credentials:'same-origin',headers:{Accept:'application/json'},cache:'no-store'});
        if (response.redirected || [401,403].includes(response.status)) { const e=Error('Сессия завершилась или доступ изменился. Войдите снова, чтобы продолжить.');e.sessionEnded=true;throw e; }
        if (!(response.headers.get('content-type') || '').includes('application/json')) throw Error('Не удалось прочитать ответ. Повторите загрузку.');
        const body=await response.json();if(!response.ok || body.success===false) throw Error(body.error || 'Не удалось загрузить данные. Повторите попытку.');
        if(!body.data || typeof body.data!=='object') throw Error('Сервер не вернул данные. Повторите загрузку.');
        return body.data;
      } catch(e) { if(timedOut) throw Error('Загрузка заняла слишком много времени. Повторите попытку.'); if(e instanceof TypeError) throw Error('Нет соединения с сервером. Сохранённые данные остаются на экране.'); throw e; }
      finally { clearTimeout(timer); }
    }
    function filtersFromURL() {
      const q=new URLSearchParams(location.search);
      return {period:['7d','30d'].includes(q.get('period'))?q.get('period'):'30d',search:(q.get('search')||'').slice(0,200),source:q.get(config.kind==='orders'?'fulfillment':'source')||'',status:(q.get('status')||'').slice(0,120),page:positive(q.get('page'))};
    }
    return {
      directives:{'image-deadline':global.mcatShared.imageDeadline},
      data:()=>({...refreshData,config,kind:config.kind,period:'30d',filters:filtersFromURL(),observedFilters:null,searchDraft:'',
        items:[],pagination:{page:1,pages:0,total:0},statusCounts:{},loading:false,loaded:false,error:'',sessionEnded:false,
        lastCompleted:null,syncState:null,failedImages:{},detailId:null,detail:null,detailLoading:false,detailError:'',detailPage:1}),
      computed:{
        sourceOptions() { return (this.kind==='orders'?['fbo','fbs']:this.kind==='returns'?['fbo_fbs','rfbs']:['posting_fbo','posting_fbs','rfbs_conditional']).map(value=>({value,label:sources[value]})); },
        hasFilters() { return Boolean(this.filters.search || this.filters.source || this.filters.status); },
        statusOptions() { const keys=new Set(Object.keys(this.statusCounts));if(this.filters.status)keys.add(this.filters.status);return [...keys].sort().map(value=>({value,label:this.statusLabel(value),count:this.statusCounts[value]||0})); },
        resultsLabel() { return new Intl.NumberFormat('ru-RU').format(this.pagination.total)+' '+({orders:'отправлений',returns:'возвратов',cancellations:'отмен'}[this.kind]); },
        freshness() { return this.lastCompleted?.completed_at ? 'Данные обновлены '+date(this.lastCompleted.completed_at) : 'Полная загрузка за выбранный период ещё не подтверждена'; },
        staleScope() { return this.observedFilters && JSON.stringify(this.filters)!==JSON.stringify(this.observedFilters); },
        loginUrl() { return '/login?next='+encodeURIComponent(location.pathname+location.search); },
      },
      methods:{...refreshMethods,date,money,safeImage,
        statusLabel(value,label) { return label || statuses[value]?.[0] || value || 'Статус не указан'; },
        tone(value) { return statuses[value]?.[1] || 'muted'; },
        sourceLabel(item) { const value=item.source_kind||item.fulfillment_kind;return sources[value]||value||'Схема не указана'; },
        lineName(line) { return line.name||line.product_name||line.offer_id||(line.external_sku?'SKU '+line.external_sku:'Товар без названия'); },
        rowDate(item) { return date(this.kind==='orders' ? item.upstream_created_at : item.status_changed_at||item.requested_at||item.upstream_created_at||item.last_seen_at); },
        orderSearchURL(number) { const q=new URLSearchParams({account_id:config.accountId,period:this.period,search:number});return config.pages.orders+'?'+q; },
        url(filters=this.filters, postingId=null, linePage=1) {
          const q=new URLSearchParams({account_id:config.accountId,period:filters.period});
          for(const key of ['search','status']) if(filters[key])q.set(key,filters[key]);
          if(filters.source)q.set(this.kind==='orders'?'fulfillment':'source',filters.source);
          if(filters.page>1)q.set('page',filters.page);
          if(postingId) {q.set('posting_id',postingId);if(linePage>1)q.set('line_page',linePage);}
          return config.pages[this.kind]+'?'+q;
        },
        async load() {
          if(!alive || this.sessionEnded)return;
          listController?.abort();listController=new AbortController();const revision=++listVersion;
          const scope={...this.filters};this.loading=true;this.error='';
          const q=new URLSearchParams({account_id:config.accountId,period:scope.period,page:scope.page,per_page:25,view:'compact'});
          for(const key of ['search','status'])if(scope[key])q.set(key,scope[key]);
          if(scope.source)q.set(this.kind==='orders'?'fulfillment':'source',scope.source);
          try {
            const data=await read(config.api[this.kind]+'?'+q,listController);
            if(!alive || revision!==listVersion)return;
            if(!Array.isArray(data.items) || !validPagination(data.pagination,scope.page,25) || data.scope?.account_id!==config.accountId) throw Error('Не удалось подтвердить список выбранного магазина. Повторите загрузку.');
            this.items=data.items;this.pagination=data.pagination;this.statusCounts=data.status_counts||{};this.lastCompleted=data.last_completed_sync||null;this.syncState=data.sync||null;this.observedFilters=scope;this.loaded=true;
          } catch(e) { if(alive && revision===listVersion && e.name!=='AbortError'){this.error=e.message;this.sessionEnded=Boolean(e.sessionEnded);} }
          finally { if(alive && revision===listVersion)this.loading=false; }
        },
        navigate(patch) {
          const oldPeriod=this.period;this.filters={...this.filters,...patch};this.period=this.filters.period;this.searchDraft=this.filters.search;
          this.dismissDetail(false);history.pushState({},'',this.url());this.load();if(oldPeriod!==this.period)this.changeRefreshPeriod();
        },
        applyFilters() { this.navigate({search:this.searchDraft.trim(),page:1}); },
        resetFilters() { this.navigate({search:'',source:'',status:'',page:1}); },
        pageLink(event,page) { if(event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;event.preventDefault();this.navigate({page}); },
        changePeriod(period) { this.navigate({period,page:1}); },
        async onRefreshCompleted() { await this.load(); },
        async openDetail(id, push=true, linePage=1) {
          if(!alive || this.sessionEnded)return;
          if(!Number.isSafeInteger(id)||id<=0)return;
          if(this.detailId!==id)this.detail=null;
          this.detailId=id;this.detailPage=linePage;this.detailError='';
          if(push)history.pushState({fulfillmentDetail:id},'',this.url(this.filters,id,linePage));
          await this.$nextTick();
          const panel=this.$refs.drawer;
          if(panel && !panel.open){returnFocus=document.activeElement;previousOverflow=document.body.style.overflow;panel.showModal();document.body.style.overflow='hidden';}
          detailController?.abort();detailController=new AbortController();const revision=++detailVersion;this.detailLoading=true;
          try {
            const data=await read(config.api.orders+'/'+id+'?'+new URLSearchParams({account_id:config.accountId,view:'compact',item_page:linePage,item_per_page:50}),detailController);
            if(!alive || revision!==detailVersion)return;
            if(data.id!==id || data.account_id!==config.accountId || !Array.isArray(data.items) || !validPagination(data.item_pagination,linePage,50))throw Error('Не удалось подтвердить отправление выбранного магазина.');
            this.detail=data;
          } catch(e) { if(alive && revision===detailVersion && e.name!=='AbortError'){this.detailError=e.message;this.sessionEnded=Boolean(e.sessionEnded);} }
          finally {if(alive && revision===detailVersion)this.detailLoading=false;}
        },
        changeLinePage(page) {history.replaceState(history.state,'',this.url(this.filters,this.detailId,page));this.openDetail(this.detailId,false,page);},
        dismissDetail(updateURL=true) {
          const previous=this.detailId;detailVersion++;detailController?.abort();this.detailId=null;this.detailLoading=false;
          if(this.$refs.drawer?.open){this.$refs.drawer.close();document.body.style.overflow=previousOverflow;returnFocus?.isConnected&&returnFocus.focus();}
          if(updateURL && previous){if(history.state?.fulfillmentDetail===previous)history.back();else history.replaceState({},'',this.url());}
        },
        retryImages() { this.failedImages={}; },
      },
      mounted() {
        document.getElementById('of-fallback')?.remove();this.period=this.filters.period;this.searchDraft=this.filters.search;
        pop=()=>{const previous=JSON.stringify(this.filters),oldPeriod=this.period;this.filters=filtersFromURL();this.period=this.filters.period;this.searchDraft=this.filters.search;if(JSON.stringify(this.filters)!==previous)this.load();if(this.period!==oldPeriod)this.changeRefreshPeriod();const q=new URLSearchParams(location.search),id=q.get('posting_id');if(this.kind==='orders' && /^[1-9]\d*$/.test(id||''))this.openDetail(Number(id),false,positive(q.get('line_page')));else this.dismissDetail(false);};
        window.addEventListener('popstate',pop);
        this.load();this.initRefresh();
        const q=new URLSearchParams(location.search);if(this.kind==='orders' && /^[1-9]\d*$/.test(q.get('posting_id')||''))this.openDetail(Number(q.get('posting_id')),false,positive(q.get('line_page')));
      },
      beforeUnmount() {alive=false;listVersion++;detailVersion++;listController?.abort();detailController?.abort();this.destroyRefresh();window.removeEventListener('popstate',pop);this.dismissDetail(false);},
    };
  }
  global.ozonFulfillment={createOptions,money,date};
  const bootstrap=document.getElementById('of-bootstrap');
  if(bootstrap && global.Vue) global.Vue.createApp(createOptions(JSON.parse(bootstrap.textContent))).mount('#of-app');
})(window);
