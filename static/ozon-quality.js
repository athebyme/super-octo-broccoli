/* Local quality observations and durable local recompute. No marketplace writes. */
(function(global){
  'use strict';
  const states={unassessed:'Ещё не проверена',changed:'Карточка изменилась',outdated:'Пора пересчитать',observed:'Сохранённая проверка'};
  const severity={critical:'Требует внимания',warning:'Можно улучшить',good:'Хорошо',excellent:'Отлично'};
  const dimensions={attributes:'Характеристики',media:'Фотографии',description:'Описание',title:'Название',barcodes:'Штрихкоды',price:'Наличие цены',publication_health:'Публикация'};
  const id=v=>/^[1-9]\d*$/.test(String(v||''))&&Number.isSafeInteger(Number(v))?Number(v):null;
  const number=v=>typeof v==='number'&&Number.isFinite(v)?new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(v):'—';
  function instant(v){if(!v)return 'Дата не указана';const d=new Date(/[Zz]|[+-]\d\d:\d\d$/.test(v)?v:v+'Z');return Number.isFinite(d.getTime())?new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}).format(d):'Дата не указана';}
  function day(v){const d=new Date(v+'T00:00:00Z');return /^\d{4}-\d{2}-\d{2}$/.test(v||'')&&Number.isFinite(d.getTime())?new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'}).format(d):'Дата не указана';}
  function imageURL(v){try{const u=new URL(v);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:'';}catch(_){return '';}}
  function options(config){
    let alive=true,listVersion=0,detailVersion=0,jobVersion=0,listController,detailController,jobController,poll,pop,visible;
    const storageKey='ozon_quality_selection_'+config.accountId;
    function fromURL(){const q=new URLSearchParams(location.search);return {filters:{search:(q.get('search')||'').slice(0,200),severity:q.get('severity')||'',reason:q.get('reason')||'',state:q.get('state')||'',sort_by:q.get('sort_by')||'priority',sort_dir:q.get('sort_dir')||'desc',page:Math.min(100000,id(q.get('page'))||1)},detailId:id(q.get('listing_id'))};}
    const signature=f=>JSON.stringify([f.search,f.severity,f.reason,f.state]);
    async function request(url,controller,post=false){let expired=false;const timer=setTimeout(()=>{expired=true;controller.abort();},10000);
      try{const r=await fetch(url,{method:post?'POST':'GET',signal:controller.signal,credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json',...(post?{'Content-Type':'application/json','X-CSRFToken':config.csrfToken}:{})},...(post?{body:'{}'}:{})});
        if(r.redirected||[401,403].includes(r.status))throw Object.assign(Error('Сессия завершилась или доступ изменился. Войдите снова.'),{sessionEnded:true});
        if(!(r.headers.get('content-type')||'').includes('application/json'))throw Error('Не удалось прочитать ответ. Повторите проверку.');
        const body=await r.json();if(!r.ok||body.success!==true)throw Error(body.error||'Не удалось выполнить запрос. Повторите позже.');return body;
      }catch(e){if(expired)throw Error('Ответ занял слишком много времени. Повторите проверку.');if(e instanceof TypeError)throw Error('Нет соединения с сервером. Проверьте подключение.');throw e;}finally{clearTimeout(timer);}
    }
    function validItem(item){return item&&id(item.listing_id)===item.listing_id&&item.account_id===config.accountId&&item.marketplace_code==='ozon'&&item.entity_kind==='marketplace_listing'&&Object.hasOwn(states,item.state)&&Array.isArray(item.reasons)&&item.reasons.length<=50&&item.url===`/marketplaces/listings/view/${item.listing_id}?account_id=${config.accountId}`&&(item.score===null||typeof item.score==='number'&&Number.isFinite(item.score)&&item.score>=0&&item.score<=100);}
    return {
      directives:{'image-deadline':global.mcatShared?.imageDeadline||{}},
      data(){const current=fromURL();return {...current,config,searchDraft:current.filters.search,items:[],summary:null,pagination:{page:1,per_page:25,pages:0,total:0},observedFilters:null,
        loading:false,loaded:false,error:'',sessionEnded:false,selected:[],selectionMessage:'',failedImages:{},detail:null,detailLoading:false,detailError:'',
        job:null,jobLoading:false,jobSending:false,jobUncertain:false,jobError:'',jobCompleted:false};},
      computed:{
        stale(){return this.observedFilters&&JSON.stringify(this.filters)!==JSON.stringify(this.observedFilters);},
        allSelected(){return this.items.length>0&&this.items.every(r=>this.selected.includes(r.listing_id));},
        someSelected(){return !this.allSelected&&this.items.some(r=>this.selected.includes(r.listing_id));},
        loginURL(){return '/login?next='+encodeURIComponent(location.pathname+location.search);},
        canRefresh(){return !this.sessionEnded&&!this.jobSending&&!this.jobLoading&&!this.jobError&&!this.jobUncertain&&!this.job?.active;},
        reasonOptions(){return [...(this.summary?.reasons||[])].sort((a,b)=>b.count-a.count);},
        catalogURL(){return '/marketplaces/listings/?account_id='+config.accountId;},
        analyticsURL(){return '/marketplaces/analytics?account_id='+config.accountId;},
        selectedCount(){return this.selected.length;},
      },
      methods:{number,instant,imageURL,day,
        stateLabel(item){return states[item.state]||'Нет оценки';},
        severityLabel(value){return severity[value]||'Нет оценки';},
        dimensionLabel(value){return dimensions[value]||value;},
        url(filters=this.filters,detailId=this.detailId){const q=new URLSearchParams({account_id:config.accountId});for(const k of ['search','severity','reason','state'])if(filters[k])q.set(k,filters[k]);if(filters.page>1)q.set('page',filters.page);if(filters.sort_by!=='priority')q.set('sort_by',filters.sort_by);if(filters.sort_dir!=='desc')q.set('sort_dir',filters.sort_dir);if(detailId)q.set('listing_id',detailId);return config.page+'?'+q;},
        endSession(error){if(!error.sessionEnded)return;this.sessionEnded=true;clearTimeout(poll);jobController?.abort();},
        async load(){if(!alive||this.sessionEnded)return;listController?.abort();const controller=listController=new AbortController(),version=++listVersion,scope={...this.filters};this.loading=true;this.error='';
          const query=new URLSearchParams({account_id:config.accountId,...scope,per_page:25});
          try{const {data}=await request(config.api+'?'+query,controller);if(!alive||this.sessionEnded||version!==listVersion)return;
            const p=data?.pagination;if(data?.scope?.account_id!==config.accountId||data.scope.marketplace_code!=='ozon'||!p||p.page!==scope.page||p.per_page!==25||!Number.isSafeInteger(p.total)||p.total<0||p.pages!==Math.ceil(p.total/25)||!Array.isArray(data.items)||data.items.length>25||data.items.some(r=>!validItem(r))||new Set(data.items.map(r=>r.listing_id)).size!==data.items.length||!data.summary||!Array.isArray(data.summary.reasons))throw Error('Не удалось подтвердить данные выбранного магазина. Повторите загрузку.');
            for(const k of ['search','severity','reason','state','sort_by','sort_dir'])if(data.filters?.[k]!==scope[k])throw Error('Пришла другая выборка. Повторите загрузку.');
            this.items=data.items;this.summary=data.summary;this.pagination=p;this.observedFilters=scope;this.loaded=true;this.jobCompleted=false;
          }catch(e){if(alive&&version===listVersion&&e.name!=='AbortError'){this.error=e.message;this.endSession(e);}}
          finally{if(alive&&version===listVersion)this.loading=false;}
        },
        navigate(patch){const previous=signature(this.filters);this.filters={...this.filters,...patch};this.searchDraft=this.filters.search;this.closeDetail(false);if(signature(this.filters)!==previous){this.selected=[];this.persistSelection();}history.pushState({},'',this.url());this.load();},
        applySearch(){this.navigate({search:this.searchDraft.trim(),page:1});},
        reset(){this.navigate({search:'',severity:'',reason:'',state:'',page:1});},
        pageLink(e,page){if(e.metaKey||e.ctrlKey||e.altKey||e.shiftKey)return;e.preventDefault();this.navigate({page});},
        toggle(listingId){this.selectionMessage='';if(this.selected.includes(listingId))this.selected=this.selected.filter(v=>v!==listingId);else if(this.selected.length<200)this.selected=[...this.selected,listingId];else this.selectionMessage='Можно выбрать до 200 карточек. Сначала снимите часть выбора.';this.persistSelection();},
        togglePage(){if(this.stale||this.loading)return;const next=this.allSelected?this.selected.filter(v=>!this.items.some(r=>r.listing_id===v)):[...new Set([...this.selected,...this.items.map(r=>r.listing_id)])];if(next.length>200){this.selectionMessage='Выбор превысит 200 карточек. Сначала снимите часть выбора.';return;}this.selected=next;this.selectionMessage='';this.persistSelection();},
        clearSelection(){this.selected=[];this.selectionMessage='';this.persistSelection();},
        persistSelection(){try{sessionStorage.setItem(storageKey,JSON.stringify({signature:signature(this.filters),ids:this.selected}));}catch(_){this.selectionMessage='Браузер не сохранил выбор. Он доступен до закрытия страницы.';}},
        restoreSelection(){try{const v=JSON.parse(sessionStorage.getItem(storageKey)||'null');if(v?.signature===signature(this.filters)&&Array.isArray(v.ids)&&v.ids.length<=200&&v.ids.every(n=>id(n)===n)&&new Set(v.ids).size===v.ids.length)this.selected=v.ids;}catch(_){}},
        saveCollection(){if(!this.selected.length||this.stale)return;try{sessionStorage.setItem('seller_hub_marketplace_collection',JSON.stringify({entity_kind:'marketplace_listing',marketplace_code:'ozon',account_id:config.accountId,listing_ids:this.selected}));location.href='/agents?new=1';}catch(_){this.selectionMessage='Не удалось передать выбор. Разрешите хранилище сайта и повторите.';}},
        async openDetail(listingId,push=true){if(!id(listingId)||this.sessionEnded)return;const changed=this.detailId!==listingId;this.detailId=listingId;if(changed)this.detail=null;if(push)history.pushState({},'',this.url());await this.$nextTick();if(!alive)return;if(this.$refs.dialog&&!this.$refs.dialog.open)this.$refs.dialog.showModal();await this.loadDetail();},
        async loadDetail(){if(!alive||this.sessionEnded||!this.detailId)return;detailController?.abort();const controller=detailController=new AbortController(),version=++detailVersion,wanted=this.detailId;this.detailLoading=true;this.detailError='';
          try{const {data}=await request(config.api+'/'+wanted+'?account_id='+config.accountId,controller);if(!alive||this.sessionEnded||version!==detailVersion||wanted!==this.detailId)return;if(!validItem(data)||data.listing_id!==wanted||!data.breakdown||!Array.isArray(data.metrics))throw Error('Не удалось подтвердить выбранную карточку. Повторите загрузку.');this.detail=data;
          }catch(e){if(alive&&version===detailVersion&&e.name!=='AbortError'){this.detailError=e.message;this.endSession(e);}}
          finally{if(alive&&version===detailVersion)this.detailLoading=false;}
        },
        closeDetail(push=true){detailVersion++;detailController?.abort();this.$refs?.dialog?.close();this.detailId=null;this.detail=null;this.detailLoading=false;this.detailError='';if(push)history.pushState({},'',this.url());},
        scheduleJob(){clearTimeout(poll);if(alive&&!this.sessionEnded&&!document.hidden&&(this.job?.active||this.jobUncertain))poll=setTimeout(()=>this.loadJob(),this.jobError?15000:5000);},
        acceptJob(body){if(body?.account_id!==config.accountId||body.marketplace_code!=='ozon'||body.job!==null&&(!body.job||body.job.account_id!==config.accountId||body.job.marketplace_code!=='ozon'||typeof body.job.job_uid!=='string'||!body.job.job_uid.startsWith('oq:'+config.accountId+':')||!['pending','running','completed','failed','paused'].includes(body.job.status)||body.job.active!==['pending','running'].includes(body.job.status)||!Number.isSafeInteger(body.job.processed)||body.job.processed<0))throw Error('Не удалось подтвердить состояние пересчёта. Проверьте его ещё раз.');
          const watched=this.job?.active||this.jobUncertain;this.job=body.job;if(watched&&body.job?.status==='completed')this.jobCompleted=true;this.jobUncertain=false;
        },
        async loadJob(){if(!alive||this.sessionEnded||this.jobSending)return;clearTimeout(poll);jobController?.abort();const controller=jobController=new AbortController(),version=++jobVersion;this.jobLoading=true;this.jobError='';
          try{const body=await request(config.refresh+'?account_id='+config.accountId,controller);if(alive&&!this.sessionEnded&&version===jobVersion)this.acceptJob(body);}
          catch(e){if(alive&&version===jobVersion&&e.name!=='AbortError'){this.jobError=e.message;this.endSession(e);}}
          finally{if(alive&&version===jobVersion){this.jobLoading=false;this.scheduleJob();}}
        },
        async startRefresh(){if(!this.canRefresh||!alive)return;clearTimeout(poll);jobController?.abort();const controller=jobController=new AbortController(),version=++jobVersion;this.jobSending=true;this.jobUncertain=true;this.jobCompleted=false;this.jobError='';
          try{const body=await request(config.refresh+'?account_id='+config.accountId,controller,true);if(alive&&!this.sessionEnded&&version===jobVersion)this.acceptJob(body);}
          catch(e){if(alive&&version===jobVersion&&e.name!=='AbortError'){this.jobError=e.message;this.endSession(e);}}
          finally{if(alive&&version===jobVersion){this.jobSending=false;this.scheduleJob();}}
        },
        retryImages(){this.failedImages={};},
      },
      mounted(){document.getElementById('oqw-fallback')?.remove();this.restoreSelection();this.load();this.loadJob();if(this.detailId)this.openDetail(this.detailId,false);
        pop=()=>{const next=fromURL(),previous=signature(this.filters);this.filters=next.filters;this.searchDraft=this.filters.search;if(previous!==signature(this.filters)){this.selected=[];this.restoreSelection();}if(next.detailId)this.openDetail(next.detailId,false);else this.closeDetail(false);this.load();};
        visible=()=>{clearTimeout(poll);if(document.hidden){if(!this.jobSending)jobController?.abort();}else if(this.job?.active||this.jobUncertain)this.loadJob();};
        global.addEventListener('popstate',pop);document.addEventListener('visibilitychange',visible);
      },
      beforeUnmount(){alive=false;listVersion++;detailVersion++;jobVersion++;listController?.abort();detailController?.abort();jobController?.abort();clearTimeout(poll);global.removeEventListener('popstate',pop);document.removeEventListener('visibilitychange',visible);},
    };
  }
  global.ozonQuality={options,number,instant,imageURL};const bootstrap=document.getElementById('oqw-bootstrap');if(bootstrap&&global.Vue)global.Vue.createApp(options(JSON.parse(bootstrap.textContent))).mount('#oqw-app');
})(window);
