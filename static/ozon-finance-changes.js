/* Local immutable observations; no provider refresh or financial writes. */
(function(global){
  'use strict';
  const kinds={added:'Появились во второй загрузке',missing:'Нет во второй загрузке',changed:'Изменились'};
  const fields={total_amount:'сумма',currency:'валюта',fact_date:'дата',unit_number:'номер отправления',accrued_category:'категория',items:'состав товаров',components:'расшифровка услуг'};
  const validId=v=>/^[1-9]\d{0,15}$/.test(String(v||''))&&Number.isSafeInteger(Number(v));
  function fromURL(){const q=new URLSearchParams(location.search);return {older:q.get('older')||'',newer:q.get('newer')||'',kind:q.get('kind')||'',page:q.get('page')||'1'};}
  function options(config){
    let alive=true,historyController,compareController,historyRevision=0,compareRevision=0,pop;
    async function read(url,controller){let expired=false;const timer=setTimeout(()=>{expired=true;controller.abort();},10000);try{
      const response=await fetch(url,{signal:controller.signal,headers:{Accept:'application/json'},credentials:'same-origin',cache:'no-store'});
      if(response.redirected||[401,403].includes(response.status))throw Object.assign(Error('Сессия завершилась. Войдите снова для просмотра финансов.'),{sessionEnded:true});
      let body;try{body=await response.json();}catch(_){throw Error('Не удалось прочитать ответ сервера. Повторите загрузку.');}
      if(!response.ok||body.success!==true)throw Error(body.error||'Не удалось сравнить загрузки. Повторите позже.');
      const data=body.data;if(data?.scope?.account_id!==config.accountId||data.scope.marketplace!=='ozon'||data.observation_only!==true)throw Error('Не удалось подтвердить выбранный магазин. Повторите загрузку.');return data;
    }catch(e){if(expired)throw Error('Загрузка заняла слишком много времени. Повторите позже.');if(e instanceof TypeError)throw Error('Нет соединения с сервером. Предыдущее сравнение остаётся на экране.');throw e;}finally{clearTimeout(timer);}}
    return {
      data:()=>({config,selection:fromURL(),history:[],historyTruncated:false,historyLoaded:false,historyLoading:false,loading:false,error:'',sessionEnded:false,data:null,observedSelection:null,kinds}),
      computed:{
        busy(){return this.loading||this.historyLoading;},
        stale(){return this.observedSelection&&JSON.stringify(this.observedSelection)!==JSON.stringify(this.selection);},
        financeUrl(){return '/marketplaces/finance?account_id='+config.accountId;},
        resetUrl(){return config.page+'?account_id='+config.accountId;},
        loginUrl(){return '/login?next='+encodeURIComponent(location.pathname+location.search);},
      },
      methods:{money:global.ozonFinance.money,date:global.ozonFinance.date,day:global.ozonFinance.day,
        snapshotLabel(row){return this.date(row.completed_at)+' · '+(row.period==='7d'?'7 дней':'30 дней')+' · № '+row.id;},
        selectedPeriod(side){const row=this.history.find(s=>String(s.id)===this.selection[side]);return row?this.day(row.start)+' — '+this.day(row.end):'';},
        kindLabel(value){return kinds[value]||'Различие';},
        fieldLabel(values){return values.map(v=>fields[v]||'данные начисления').join(', ');},
        url(scope=this.selection){const q=new URLSearchParams({account_id:config.accountId});for(const k of ['older','newer','kind'])if(scope[k])q.set(k,scope[k]);if(scope.page!=='1')q.set('page',scope.page);return config.page+'?'+q;},
        factUrl(side,row){if(!this.data||!row)return this.financeUrl;const snapshot=this.data[side];return '/marketplaces/finance?'+new URLSearchParams({account_id:config.accountId,period:snapshot.period,snapshot_id:snapshot.id,as_of:snapshot.end,fact_id:row.id});},
        async loadHistory(){
          if(!alive||this.sessionEnded)return;
          historyController?.abort();const controller=historyController=new AbortController(),revision=++historyRevision;this.historyLoading=true;this.error='';
          const q=new URLSearchParams({account_id:config.accountId});if(this.selection.newer)q.set('anchor_id',this.selection.newer);
          try{const data=await read(config.historyApi+'?'+q,controller);if(!alive||revision!==historyRevision)return;
            if(!Array.isArray(data.items)||data.items.length>16||data.items.some(s=>!Number.isSafeInteger(s.id)||s.id<=0)||data.anchor&&(String(data.anchor.id)!==this.selection.newer))throw Error('История загрузок не подтверждена. Повторите позже.');
            this.history=data.items;this.historyTruncated=!!data.truncated;this.historyLoaded=true;
            if(data.anchor&&!this.history.some(s=>s.id===data.anchor.id))this.history.push(data.anchor);
            if(!this.selection.newer&&this.history.length)this.selection.newer=String(this.history[0].id);
            if(!this.selection.older){const newer=this.history.find(s=>String(s.id)===this.selection.newer),older=this.history.find(s=>newer&&(s.completed_at<newer.completed_at||s.completed_at===newer.completed_at&&s.id<newer.id)&&s.start<=newer.end&&s.end>=newer.start);if(older)this.selection.older=String(older.id);}
            if(this.selection.older&&this.selection.newer){history.replaceState({},'',this.url());await this.load();}
          }catch(e){if(alive&&revision===historyRevision&&e.name!=='AbortError'){this.error=e.message;this.sessionEnded=!!e.sessionEnded;}}
          finally{if(alive&&revision===historyRevision)this.historyLoading=false;}
        },
        async load(){
          if(!alive||this.sessionEnded)return;
          compareController?.abort();const controller=compareController=new AbortController(),revision=++compareRevision,scope={...this.selection};this.loading=true;this.error='';
          try{if(!validId(scope.older)||!validId(scope.newer))throw Error('Выберите две сохранённые загрузки.');
            const q=new URLSearchParams({account_id:config.accountId,older:scope.older,newer:scope.newer,kind:scope.kind,page:scope.page,per_page:50});const data=await read(config.api+'?'+q,controller);if(!alive||revision!==compareRevision)return;
            if(data.accounting_reconciliation!==false||String(data.older?.id)!==scope.older||String(data.newer?.id)!==scope.newer||data.filter!==scope.kind||data.period?.common_dates_only!==true||!Array.isArray(data.items)||data.items.length>50||!Array.isArray(data.totals)||!data.counts||data.pagination?.page!==Number(scope.page)||data.pagination.per_page!==50||!Number.isSafeInteger(data.pagination.total)||data.pagination.total<0||data.pagination.pages!==Math.ceil(data.pagination.total/50)||data.items.some(r=>!kinds[r.kind]||!Array.isArray(r.fields)||[r.older,r.newer].some(f=>f&&(!Number.isSafeInteger(f.id)||f.id<=0))))throw Error('Получено другое или неполное сравнение. Повторите загрузку.');
            this.data=data;this.observedSelection=scope;
          }catch(e){if(alive&&revision===compareRevision&&e.name!=='AbortError'){this.error=e.message;this.sessionEnded=!!e.sessionEnded;}}
          finally{if(alive&&revision===compareRevision)this.loading=false;}
        },
        run(){this.selection={...this.selection,kind:'',page:'1'};history.pushState({},'',this.url());this.load();},
        filter(kind){if(this.stale||this.busy)return;this.selection={...this.selection,kind,page:'1'};history.pushState({},'',this.url());this.load();},
        pageLink(event,page){if(event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;event.preventDefault();if(this.stale||this.busy)return;this.selection={...this.selection,page:String(page)};history.pushState({},'',this.url());this.load();},
      },
      mounted(){document.getElementById('ofc-fallback')?.remove();pop=()=>{historyRevision++;compareRevision++;historyController?.abort();compareController?.abort();this.loading=false;this.historyLoading=false;this.selection=fromURL();this.loadHistory();};window.addEventListener('popstate',pop);this.loadHistory();},
      beforeUnmount(){alive=false;historyRevision++;compareRevision++;historyController?.abort();compareController?.abort();window.removeEventListener('popstate',pop);},
    };
  }
  global.ozonFinanceChanges={options};const node=document.getElementById('ofc-bootstrap');if(node&&global.Vue)global.Vue.createApp(options(JSON.parse(node.textContent))).mount('#ofc-app');
})(window);
