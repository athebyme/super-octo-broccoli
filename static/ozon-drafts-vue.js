/* Draft workspace: explicit source selection, one-account bulk review, Vue 3. */
(function (global) {
    'use strict';
    const selectionKey = 'ozon-drafts-selection-v1';
    const aiSelectionKey = 'ozon-drafts-ai-selection-v1', aiPendingKey = 'ozon-drafts-ai-pending-v1';
    const aiUid = /^ozon-ai-[0-9a-f]{32}$/;
    const positive = value => Number.isSafeInteger(value) && value > 0;
    const readStored = key => { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_) { return null; } };
    const hasStored = key => { try { return sessionStorage.getItem(key) !== null; } catch (_) { return false; } };
    const saveStored = (key,value) => { try { sessionStorage.setItem(key, JSON.stringify(value)); return true; } catch (_) { return false; } };
    const clearStored = key => { try { sessionStorage.removeItem(key); } catch (_) {} };
    function aiRequestKey() {
        return global.crypto.randomUUID?.().replace(/-/g,'') || Array.from(
            global.crypto.getRandomValues(new Uint8Array(16)),
            byte => byte.toString(16).padStart(2,'0')).join('');
    }
    function restoreAiSelection(accounts) {
        const value = readStored(aiSelectionKey);
        if (!value || !positive(value.account_id) || !accounts.some(row => row.id === value.account_id) ||
            !Array.isArray(value.rows) || value.rows.length > 200) return null;
        const seen = new Set();
        if (!value.rows.every(row => row && positive(row.id) && positive(row.version) &&
                !seen.has(row.id) && (seen.add(row.id), true))) return null;
        return value;
    }
    function validAiPending(value, accounts) {
        if (!value || !positive(value.account_id) || !accounts.some(row => row.id === value.account_id) ||
            typeof value.request_key !== 'string' || !/^[A-Za-z0-9_-]{24,128}$/.test(value.request_key) ||
            !Array.isArray(value.draft_ids) || !value.draft_ids.length || value.draft_ids.length > 200 ||
            !value.draft_ids.every(positive) || new Set(value.draft_ids).size !== value.draft_ids.length ||
            !value.expected_versions || typeof value.expected_versions !== 'object' || Array.isArray(value.expected_versions)) return false;
        const keys = Object.keys(value.expected_versions).sort();
        return JSON.stringify(keys) === JSON.stringify(value.draft_ids.map(String).sort()) &&
            Object.values(value.expected_versions).every(positive);
    }
    function restoreSelection(accounts) {
        try {
            const value = JSON.parse(sessionStorage.getItem(selectionKey) || 'null');
            if (!value || !Number.isSafeInteger(value.account_id) ||
                !accounts.some(account => account.id === value.account_id) ||
                !Array.isArray(value.draft_ids) || value.draft_ids.length > 200) return null;
            const ids = value.draft_ids.filter(id => Number.isSafeInteger(id) && id > 0);
            return ids.length === new Set(ids).size ? {account_id:value.account_id,draft_ids:ids} : null;
        } catch (_) { return null; }
    }
    function createOptions(config) {
        const picker = global.draftSourcePicker(config.urls.sources, config.sourceSearch);
        const methods = {}, state = {};
        for (const [key, value] of Object.entries(picker)) (typeof value === 'function' ? methods : state)[key] = value;
        let outside, alive = true;
        const saved = restoreSelection(config.accounts);
        const savedAi = restoreAiSelection(config.accounts), pendingAi = readStored(aiPendingKey);
        return {
            data() { return {...state, config, rows:config.rows, accounts:config.accounts, selectedDrafts:saved?.draft_ids || [], selectedAccountId:saved?.account_id || null,
                createAccountId:config.filters.account_id || (config.accounts.length === 1 ? config.accounts[0].id : ''), offerId:'',
                actionError:'', actionUncertain:false, busy:'', failedImages:[], selectionMode:'send',
                aiSelected:savedAi?.rows || [], aiAccountId:savedAi?.account_id || null,
                aiPending:validAiPending(pendingAi,config.accounts) ? pendingAi : null,
                aiStorageProblem:hasStored(aiPendingKey) && !validAiPending(pendingAi,config.accounts),
                aiRetrySameKey:false, aiSessionEnded:false, aiBusy:'', aiError:'', aiConfirmed:false,
                aiCsrf:config.csrf}; },
        computed: {
                selectedAccount() { return this.accounts.find(account => account.id === this.selectedAccountId); },
                bulkReady() { return this.selectedDrafts.length > 0 && this.selectedDrafts.length <= 200 && !!this.selectedAccount; },
                aiSelectedAccount() { return this.accounts.find(account => account.id === this.aiAccountId); },
                aiReady() { return this.aiSelected.length > 0 && this.aiSelected.length <= 200 &&
                    !!this.aiSelectedAccount && !this.aiPending && !this.aiStorageProblem && !this.aiSessionEnded; },
            },
            methods: {
                ...methods,
                stateLabel(row) { return {needs_category:'Нужна категория',draft:'На подготовке',blocked:'Нужны исправления',ready:'Готово по сохранённым данным',published:'Опубликован',archived:'Архив'}[row.status] || 'Черновик'; },
                stateTone(row) { return {needs_category:'warn',blocked:'danger',ready:'muted',published:'ok'}[row.status] || 'muted'; },
                validationLabel(row) {
                    if (row.validation_status === 'stale') return 'Проверьте после изменений';
                    if (row.validation_status === 'never_validated') return 'Ещё не проверялся';
                    const count = row.validation_summary?.error_count || 0;
                    if (count) return 'Замечаний в сохранённой проверке: ' + count;
                    if (row.validation_status === 'valid') {
                        const date = this.validationDate(row.validated_at);
                        return date ? 'Проверка сохранена ' + date : 'Проверка сохранена';
                    }
                    return 'Нужна проверка';
                },
                validationDate(value) {
                    if (typeof value !== 'string') return '';
                    const match = value.match(/^(\d{4})-(\d{2})-(\d{2})/);
                    return match ? match[3] + '.' + match[2] + '.' + match[1] : '';
                },
                eligible(row) {
                    const account = this.accounts.find(item => item.id === row.account_id);
                    if (!config.enabled || !config.publicationEnabled || !account?.can_publish) return false;
                    if (account.credential_expires_at && new Date(account.credential_expires_at + (/Z$|[+-]\d\d:\d\d$/.test(account.credential_expires_at) ? '' : 'Z')).getTime() <= Date.now()) return false;
                    return row.status === 'ready' && Number.isSafeInteger(row.version) && row.version > 0 && row.validation_summary?.publishable === true;
                },
                toggleRow(row, checked) {
                    if (this.busy || this.actionUncertain) return;
                    if (!checked) {
                        this.selectedDrafts = this.selectedDrafts.filter(id => id !== row.id);
                        if (!this.selectedDrafts.length) this.selectedAccountId = null;
                    } else {
                        if (!this.eligible(row) || (this.selectedAccountId !== null && this.selectedAccountId !== row.account_id) || this.selectedDrafts.length >= 200 || this.selectedDrafts.includes(row.id)) return;
                        this.selectedAccountId = row.account_id; this.selectedDrafts.push(row.id);
                    }
                    this.persistSelection();
                },
                persistSelection() {
                    try {
                        if (!this.selectedDrafts.length) sessionStorage.removeItem(selectionKey);
                        else sessionStorage.setItem(selectionKey, JSON.stringify({
                            account_id:this.selectedAccountId, draft_ids:this.selectedDrafts,
                        }));
                    } catch (_) { this.actionError = 'Браузер не сохранил выбор между страницами. Проверьте настройки хранения данных сайта.'; }
                },
                clearSelection() { if (!this.busy) { this.selectedDrafts = []; this.selectedAccountId = null; this.persistSelection(); } },
                imageUrl(row) {
                    const value = row.primary_image;
                    if (typeof value !== 'string' || this.failedImages.includes(row.id)) return '';
                    if (/^\/api\/photos\/imported-product\/\d+\/0$/.test(value)) return value;
                    try { const url = new URL(value); return ['http:','https:'].includes(url.protocol) && !url.username && !url.password ? value : ''; } catch (_) { return ''; }
                },
                editorUrl(row) { return config.urls.editorBase + row.id; },
                async write(url, body) {
                    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 45000);
                    try {
                        const response = await fetch(url, {method:'POST',credentials:'same-origin',signal:controller.signal,headers:{Accept:'application/json','Content-Type':'application/json','X-CSRFToken':config.csrf},body:JSON.stringify(body)});
                        if (response.status === 401 || response.redirected) throw Object.assign(Error('Сессия истекла. Войдите снова в другой вкладке.'), {status:401});
                        if (!(response.headers.get('content-type') || '').includes('application/json')) throw Error('Не удалось подтвердить результат. Обновите список перед повторным действием.');
                        const data = await response.json();
                        if (!response.ok || data.success === false) throw Object.assign(Error(data.error || 'Не удалось выполнить действие.'), {status:response.status});
                        return data;
                    } finally { clearTimeout(timer); }
                },
                failure(error) {
                    this.actionUncertain = !error.status || error.status >= 500;
                    this.actionError = error.name === 'AbortError' ? 'Сервер не ответил вовремя. Проверьте список черновиков и историю загрузок перед повтором.' : error instanceof TypeError ? 'Нет ответа от сервера. Проверьте соединение и сохранённое состояние.' : error.message;
                },
                async createDraft(event) {
                    this.submit(event);
                    if (!this.selectedId || !this.createAccountId || this.busy || this.actionUncertain) return;
                    this.busy = 'create'; this.actionError = '';
                    let focusError = false;
                    try {
                        const data = await this.write(config.urls.create, {account_id:Number(this.createAccountId),imported_product_id:Number(this.selectedId),offer_id:this.offerId || undefined,validate:true});
                        if (!Number.isSafeInteger(data.draft?.id) || data.draft.id <= 0) throw Error('Результат подготовки не подтверждён. Обновите список черновиков.');
                        if (alive) { this.actionUncertain = true; global.location.assign(this.editorUrl(data.draft)); }
                    } catch (error) { if (alive) { this.failure(error); focusError = true; } }
                    finally { if (alive) this.busy = ''; }
                    if (focusError && alive) await new Promise(resolve => this.$nextTick(resolve));
                    if (focusError && alive) this.$refs.actionError?.focus();
                },
                reviewBulk() {
                    if (!this.bulkReady || this.busy || this.actionUncertain) return;
                    const query = new URLSearchParams({
                        account_id:String(this.selectedAccountId), draft_ids:this.selectedDrafts.join(','),
                    });
                    global.location.assign(config.urls.review + '?' + query);
                },
                setSelectionMode(mode) { if (mode === 'send' || mode === 'ai') this.selectionMode = mode; },
                aiEligible(row) {
                    return config.enabled && this.accounts.some(account => account.id === row.account_id) &&
                        positive(row.version) && row.status !== 'archived';
                },
                aiPicked(row) { return this.aiSelected.some(item => item.id === row.id && item.version === row.version); },
                toggleActiveRow(row, checked) {
                    if (this.selectionMode === 'ai') this.toggleAiRow(row, checked);
                    else this.toggleRow(row, checked);
                },
                persistAiSelection() {
                    if (!this.aiSelected.length) clearStored(aiSelectionKey);
                    else if (!saveStored(aiSelectionKey, {account_id:this.aiAccountId, rows:this.aiSelected}))
                        this.aiError = 'Браузер не сохранил выбор AI между страницами.';
                },
                toggleAiRow(row, checked) {
                    if (this.aiBusy || this.aiPending || this.aiStorageProblem || this.aiSessionEnded) return;
                    if (!checked) {
                        this.aiSelected = this.aiSelected.filter(item => item.id !== row.id);
                        if (!this.aiSelected.length) this.aiAccountId = null;
                    } else {
                        if (!this.aiEligible(row) || (this.aiAccountId !== null && this.aiAccountId !== row.account_id) ||
                            this.aiSelected.length >= 200 || this.aiSelected.some(item => item.id === row.id)) return;
                        this.aiAccountId = row.account_id;
                        this.aiSelected.push({id:row.id,version:row.version});
                    }
                    this.persistAiSelection();
                },
                clearAiSelection() {
                    if (this.aiBusy || this.aiPending) return;
                    this.aiSelected = []; this.aiAccountId = null; this.persistAiSelection();
                },
                clearAiStorageProblem() {
                    if (!this.aiStorageProblem || this.aiBusy) return;
                    clearStored(aiPendingKey); this.aiStorageProblem = false;
                    this.aiError = 'Повреждённая запись снята после проверки истории. Просмотрите карточки перед новым запуском.';
                },
                openAiDialog() {
                    if (!this.aiReady || this.aiBusy || this.busy) return;
                    this.aiConfirmed = false; this.$refs.aiDialog.showModal();
                    this.$nextTick(() => this.$refs.aiConfirm?.focus());
                },
                closeAiDialog() {
                    if (this.aiBusy === 'post') return;
                    this.$refs.aiDialog?.close(); this.aiConfirmed = false; this.$refs.aiButton?.focus();
                },
                async aiRequest(url, options={}) {
                    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), options.method === 'POST' ? 45000 : 10000);
                    try {
                        const response = await fetch(url, {method:options.method || 'GET', credentials:'same-origin',
                            redirect:'manual', cache:'no-store', signal:abort.signal,
                            headers:{Accept:'application/json', ...(options.method === 'POST' ?
                                {'Content-Type':'application/json','X-CSRFToken':this.aiCsrf} : {}), ...(options.headers || {})},
                            ...(options.body ? {body:JSON.stringify(options.body)} : {})});
                        if (response.status === 401 || response.type === 'opaqueredirect' || response.redirected)
                            throw Object.assign(Error('Сессия завершилась. Войдите снова и проверьте сохранённый результат.'), {status:401});
                        if (response.status === 403)
                            throw Object.assign(Error('Доступ к магазину больше недоступен.'), {status:403});
                        if (!(response.headers.get('content-type') || '').includes('application/json'))
                            throw Error('Ответ сервера не подтверждён. Проверьте результат по сохранённому ключу.');
                        const body = await response.json();
                        if (typeof body.csrf_token === 'string' && body.csrf_token) this.aiCsrf = body.csrf_token;
                        if (!response.ok || body.success !== true)
                            throw Object.assign(Error(body.error || 'Не удалось выполнить действие.'), {status:response.status});
                        return body;
                    } finally { clearTimeout(timer); }
                },
                async startAi() {
                    if (!this.aiReady || !this.aiConfirmed || this.aiBusy || this.busy) return;
                    const pending = {account_id:this.aiAccountId,
                        draft_ids:this.aiSelected.map(row => row.id),
                        expected_versions:Object.fromEntries(this.aiSelected.map(row => [String(row.id),row.version])),
                        request_key:aiRequestKey()};
                    if (!saveStored(aiPendingKey,pending)) {
                        this.aiError = 'Браузер не сохранил ключ запуска. Действие остановлено.';
                        this.closeAiDialog(); return;
                    }
                    this.aiPending = pending;
                    await this.submitAiPending(pending);
                },
                async retryAiPending() {
                    if (!this.aiPending || !this.aiRetrySameKey || this.aiBusy || this.aiSessionEnded) return;
                    this.aiRetrySameKey = false;
                    await this.submitAiPending(this.aiPending);
                },
                async submitAiPending(pending) {
                    if (!validAiPending(pending,this.accounts) || this.aiBusy || this.aiSessionEnded) return;
                    this.aiBusy = 'post'; this.aiError = '';
                    try {
                        const data = await this.aiRequest(config.urls.aiCreate, {method:'POST',body:{
                            ...pending,confirm_generate:true}});
                        if (data.run?.mode !== 'draft_suggestions' || data.run.account_id !== pending.account_id ||
                            !aiUid.test(data.run.job_uid)) throw Error('Ответ не совпал с выбранным магазином и действием.');
                        clearStored(aiPendingKey); this.aiPending = null;
                        global.location.assign(config.urls.aiRunBase + data.run.job_uid);
                    } catch (error) {
                        if (error.status === 401 || error.status === 403) this.aiSessionEnded = true;
                        if (error.status && error.status < 500 && error.status !== 401 && error.status !== 403) {
                            clearStored(aiPendingKey); this.aiPending = null;
                        }
                        this.aiError = error.name === 'AbortError' ? 'Ответ запуска неизвестен. Проверьте результат по сохранённому ключу.' :
                            error instanceof TypeError ? 'Нет связи. Проверьте результат по сохранённому ключу.' : error.message;
                    } finally { this.aiBusy = ''; this.$refs.aiDialog?.close(); this.aiConfirmed = false; }
                },
                async readAiPending() {
                    if (!this.aiPending || this.aiBusy || this.aiSessionEnded) return;
                    this.aiBusy = 'read'; this.aiError = ''; this.aiRetrySameKey = false;
                    try {
                        const data = await this.aiRequest(config.urls.aiByRequest + '?account_id=' + this.aiPending.account_id,
                            {headers:{'X-AI-Request-Key':this.aiPending.request_key}});
                        if (data.run?.mode !== 'draft_suggestions' || data.run.account_id !== this.aiPending.account_id ||
                            !aiUid.test(data.run.job_uid)) throw Error('Найденный запуск относится к другому магазину.');
                        clearStored(aiPendingKey); this.aiPending = null;
                        global.location.assign(config.urls.aiRunBase + data.run.job_uid);
                    } catch (error) {
                        if (error.status === 401 || error.status === 403) this.aiSessionEnded = true;
                        if (error.status === 404) this.aiRetrySameKey = true;
                        this.aiError = error.status === 404 ? 'Запуск не найден. Это не доказывает, что первый запрос не был принят; можно вручную повторить тот же запрос с тем же ключом.' :
                            error.name === 'AbortError' ? 'Проверка заняла слишком много времени. Повторите чтение.' :
                            error instanceof TypeError ? 'Нет связи. Повторите чтение.' : error.message;
                    } finally { this.aiBusy = ''; }
                },
            },
            mounted() {
                const before = this.selectedDrafts.length;
                this.selectedDrafts = this.selectedDrafts.filter(id => {
                    const row = this.rows.find(item => item.id === id);
                    return !row || (row.account_id === this.selectedAccountId && this.eligible(row));
                });
                if (before !== this.selectedDrafts.length) this.persistSelection();
                const aiBefore = this.aiSelected.length;
                this.aiSelected = this.aiSelected.filter(item => {
                    const row = this.rows.find(candidate => candidate.id === item.id);
                    return !row || (row.account_id === this.aiAccountId && this.aiEligible(row) && row.version === item.version);
                });
                if (aiBefore !== this.aiSelected.length) {
                    if (!this.aiSelected.length) this.aiAccountId = null;
                    this.persistAiSelection();
                    this.aiError = 'Часть выбранных для AI карточек изменилась; их выбор снят. Просмотрите их снова.';
                }
                outside = event => { if (!this.$refs.sourceWrap?.contains(event.target)) this.open = false; };
                document.addEventListener('pointerdown', outside); document.getElementById('odl-boot-fallback')?.remove();
            },
            beforeUnmount() { alive = false; this.destroy(); document.removeEventListener('pointerdown', outside); },
            watch: { aiError(value) { if (value) this.$nextTick(() => this.$refs.aiError?.focus()); } },
        };
    }
    global.ozonDraftsVue = {createOptions};
    const bootstrap = document.getElementById('odl-bootstrap');
    if (bootstrap && global.Vue) global.Vue.createApp(createOptions(JSON.parse(bootstrap.textContent))).mount('#ozon-drafts-list');
})(window);
