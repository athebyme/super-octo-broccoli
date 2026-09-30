/* Read-only local diagnostics. This page cannot enqueue provider reads or writes. */
(function(global){
  'use strict';
  const domains=['catalog','analytics','fulfillment','finance','reviews','questions'];
  const paths={catalog:'listings',analytics:'analytics',fulfillment:'orders',finance:'finance',reviews:'reviews',questions:'reviews'};
  const activities={idle:'Ожидает следующего обновления',pending:'В очереди',running:'Загружаем данные',waiting:'Обновление отложено',failed:'Последняя загрузка не завершилась',paused:'Заявка требует повторного запуска',access_denied:'Ozon отклонил доступ',access_unconfirmed:'Право доступа не подтверждено',account_unavailable:'Нужно восстановить подключение',not_enabled:'Обновление выключено'};
  const accounts={connected:'Магазин подключён',disabled:'Магазин отключён',expired:'Срок действия ключа истёк',credentials_missing:'Ключ ещё не сохранён',connection_unconfirmed:'Подключение требует проверки'};
  const scheduler={healthy:'Обработчик отвечает',stale:'Обработка задач задерживается',stopped:'Обработчик остановлен',unknown:'Работу обработчика пока не удалось подтвердить',disabled:'Фоновая обработка выключена'};
  const kinds={product_import:'Создание карточки',product_import_rollback:'Отмена создания',product_update:'Изменение карточки',product_update_rollback:'Возврат карточки',price_update:'Изменение цены',price_rollback:'Возврат цены',stock_update:'Изменение остатка',stock_rollback:'Возврат остатка'};
  function instant(value){const date=value?new Date(value):null;return date&&Number.isFinite(date.getTime())?date.toLocaleString('ru-RU',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}):'Нет наблюдения';}
  function age(value){if(!Number.isFinite(value)||value<0)return 'Время неизвестно';if(value<60)return 'меньше минуты';if(value<3600)return Math.floor(value/60)+' мин';if(value<86400)return Math.floor(value/3600)+' ч';return Math.floor(value/86400)+' дн';}
  function options(config){
    let alive=true,controller=null,revision=0,timer=null,visibility=null;
    return {
      data:()=>({config,data:null,loading:false,error:'',sessionEnded:false}),
      computed:{settingsUrl(){return '/marketplaces/accounts?account_id='+config.accountId;},loginUrl(){return '/login?next='+encodeURIComponent(location.pathname+location.search);}},
      methods:{instant,age,
        accountLabel(value){return accounts[value]||accounts.connection_unconfirmed;},schedulerLabel(value){return scheduler[value]||scheduler.unknown;},activityLabel(value){return activities[value]||'Состояние не подтверждено';},kindLabel(value){return kinds[value]||'Операция Ozon';},
        domainUrl(row){return '/marketplaces/'+paths[row.domain]+'?account_id='+config.accountId+(row.domain==='questions'?'&source_kind=question':'');},
        operationUrl(row){return '/marketplaces/operations/'+row.id;},
        reviewUrl(row){return '/marketplaces/operations/'+row.id+'/review';},
        freshness(row){return {fresh:'Полная загрузка актуальна',stale:'Данные требуют обновления',unknown:'Нет подтверждённой полной загрузки'}[row.freshness]||'Свежесть неизвестна';},
        message(row){
          if(row.activity==='not_enabled')return 'Обновления выключены настройкой сервиса. Сохранённые данные остаются доступны.';
          if(row.activity==='account_unavailable')return 'Откройте настройки магазина и восстановите подключение.';
          if(row.activity==='access_unconfirmed')return 'В сохранённых правах ключа нет подтверждения этого раздела. Проверьте подключение и права.';
          if(row.activity==='access_denied')return 'Последний запрос к разделу отклонён. Проверьте права и условия доступа в Ozon; другие разделы могут работать.';
          if(row.activity==='waiting')return row.last_error_code==='account_busy'?'В магазине выполняется другая задача. Обновление продолжится после паузы.':['provider_rate_limited','ozon_rate_limited'].includes(row.last_error_code)?'Ozon ограничил частоту запросов. Очередь сохраняет указанную паузу.':'Загрузка отложена. Сохранённые данные доступны; следующая попытка назначена ниже.';
          if(row.activity==='paused')return 'Ключ или срок заявки изменился. Откройте раздел и запустите обновление заново.';
          if(row.activity==='failed')return 'Откройте раздел, чтобы проверить результат и доступный следующий шаг.';
          if(row.activity==='running')return 'Пока загрузка не завершена, ориентируйтесь на дату последнего полного обновления.';
          if(row.activity==='pending')return 'Заявка сохранена. Страницу можно закрыть; работа продолжится в фоне.';
          if(row.due_lag_seconds>900)return 'Плановое обновление задерживается. Проверьте состояние обработчика выше.';
          return row.freshness==='unknown'?'Откройте раздел и загрузите данные, если доступ к нему подтверждён.':row.freshness==='stale'?'Откройте раздел, чтобы увидеть сохранённые данные и состояние обновления.':'Это последнее сохранённое наблюдение, а не проверка Ozon в реальном времени.';
        },
        schedule(){clearTimeout(timer);if(alive&&!document.hidden&&!this.sessionEnded)timer=setTimeout(()=>this.load(),30000);},
        async load(){
          if(!alive||document.hidden||this.sessionEnded)return;
          clearTimeout(timer);controller?.abort();const current=controller=new AbortController(),version=++revision;let expired=false;
          const timeout=setTimeout(()=>{expired=true;current.abort();},10000);this.loading=true;this.error='';
          try{
            const response=await fetch(config.api+'?account_id='+config.accountId,{headers:{Accept:'application/json'},credentials:'same-origin',cache:'no-store',signal:current.signal});
            if(response.redirected||[401,403].includes(response.status))throw Object.assign(Error('Сессия завершилась. Войдите снова, чтобы проверить состояние.'),{sessionEnded:true});
            let body;try{body=await response.json();}catch(_){throw Error('Сервер не вернул состояние магазина. Повторите проверку.');}
            if(!response.ok||body.success!==true)throw Error(body.error||'Не удалось проверить состояние магазина.');
            const data=body.data;
            if(data?.scope?.account_id!==config.accountId||data.scope.marketplace!=='ozon'||data.account?.id!==config.accountId||data.observation_only!==true||data.publication_permission_evaluated!==false||!Array.isArray(data.domains)||data.domains.length!==6||new Set(data.domains.map(d=>d.domain)).size!==6||data.domains.some(d=>!domains.includes(d.domain)||!['fresh','stale','unknown'].includes(d.freshness)||!activities[d.activity])||!Array.isArray(data.operations)||data.operations.length>25||data.operations.some(o=>!Number.isSafeInteger(o.id)||o.id<=0)||!scheduler[data.scheduler?.state])throw Error('Не удалось подтвердить магазин и полноту проверки. Повторите загрузку.');
            if(alive&&version===revision)this.data=data;
          }catch(e){if(alive&&version===revision){if(expired)this.error='Проверка заняла слишком много времени. Повторите позже.';else if(e.name!=='AbortError')this.error=e instanceof TypeError?'Нет соединения с сервером. Последнее наблюдение сохранено на экране.':e.message;if(e.sessionEnded)this.sessionEnded=true;}}
          finally{clearTimeout(timeout);if(alive&&version===revision){this.loading=false;this.schedule();}}
        },
      },
      mounted(){document.getElementById('oh-fallback')?.remove();visibility=()=>{clearTimeout(timer);if(document.hidden){revision++;controller?.abort();this.loading=false;}else this.load();};document.addEventListener('visibilitychange',visibility);this.load();},
      beforeUnmount(){alive=false;revision++;controller?.abort();clearTimeout(timer);document.removeEventListener('visibilitychange',visibility);},
    };
  }
  global.ozonAccountHealth={options,age,instant};
  const bootstrap=document.getElementById('oh-bootstrap');if(bootstrap&&global.Vue)global.Vue.createApp(options(JSON.parse(bootstrap.textContent))).mount('#oh-app');
})(window);
