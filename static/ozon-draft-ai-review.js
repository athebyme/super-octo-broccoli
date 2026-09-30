/* Optional local AI suggestions. Seller confirms every field; no Ozon write here. */
(function (global) {
    'use strict';
    const uid = /^ozon-ai-[0-9a-f]{32}$/;
    const positive = value => Number.isSafeInteger(value) && value > 0;
    const read = key => { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_) { return null; } };
    const exists = key => { try { return sessionStorage.getItem(key) !== null; } catch (_) { return false; } };
    const save = (key,value) => { try { sessionStorage.setItem(key,JSON.stringify(value)); return true; } catch (_) { return false; } };
    const forget = key => { try { sessionStorage.removeItem(key); } catch (_) {} };
    const key = () => global.crypto.randomUUID?.().replace(/-/g,'') || Array.from(
        global.crypto.getRandomValues(new Uint8Array(16)),
        byte => byte.toString(16).padStart(2,'0')).join('');
    const validKey = value => typeof value === 'string' && /^[A-Za-z0-9_-]{24,128}$/.test(value);
    const same = (a,b) => JSON.stringify(a) === JSON.stringify(b);
    const component = {
        props:['draftId','accountId','version','productTypeId','dirty','locked','csrf','urls'],
        emits:['applied','suggestions','csrf'],
        data() {
            const generateKey = 'ozon-ai-single-generate-v1:'+this.draftId;
            const reviewKey = 'ozon-ai-single-review-v1:'+this.draftId;
            const generation = read(generateKey), review = read(reviewKey);
            return {doc:null, loading:false, busy:'', error:'', notice:'', sessionEnded:false,
                localCsrf:this.csrf, selected:[], confirmGenerate:false, confirmReview:false, reviewAction:'',
                generationPending:this.validGeneration(generation) ? generation : null,
                generationProblem:exists(generateKey) && !this.validGeneration(generation),
                generationRetry:false,
                reviewPending:this.validReview(review) ? review : null,
                reviewProblem:exists(reviewKey) && !this.validReview(review),
                reviewCanReplay:false};
        },
        computed: {
            available() { return this.doc?.suggestions?.filter(row => row.applicable) || []; },
            proposed() { return this.doc?.suggestions?.filter(row => row.status === 'proposed') || []; },
            accepted() { return this.doc?.suggestions?.filter(row => row.status === 'accepted').length || 0; },
            reviewLocked() { return !!this.busy || this.loading || this.sessionEnded || this.reviewPending || this.reviewProblem ||
                this.generationPending || this.generationProblem || this.dirty || this.locked || !this.doc?.review_token; },
            canGenerate() { return positive(this.productTypeId) && positive(this.version) && !this.locked &&
                !this.dirty && !this.busy && !this.loading && !this.sessionEnded &&
                !['pending','reserved'].includes(this.doc?.item?.status) && !this.generationPending &&
                !this.generationProblem && !this.reviewPending && !this.reviewProblem; },
        },
        methods: {
            generationStorage() { return 'ozon-ai-single-generate-v1:'+this.draftId; },
            reviewStorage() { return 'ozon-ai-single-review-v1:'+this.draftId; },
            validGeneration(value) { return value && value.account_id === this.accountId &&
                same(value.draft_ids,[this.draftId]) && value.expected_versions?.[String(this.draftId)] === value.viewed_version &&
                positive(value.viewed_version) && validKey(value.request_key) &&
                same(Object.keys(value.expected_versions),[String(this.draftId)]); },
            validReview(value) { return value && value.account_id === this.accountId && value.draft_id === this.draftId &&
                ['apply','reject'].includes(value.action) && positive(value.expected_version) &&
                Array.isArray(value.suggestion_ids) && value.suggestion_ids.length && value.suggestion_ids.length <= 200 &&
                value.suggestion_ids.every(positive) && new Set(value.suggestion_ids).size === value.suggestion_ids.length &&
                typeof value.review_token === 'string' && value.review_token.length > 0 &&
                value.review_token.length <= 8192 && validKey(value.request_key); },
            statusLabel(status) { return ({pending:'В очереди',reserved:'AI обрабатывает',proposed:'Предложения готовы',
                no_evidence:'Подтверждённых предложений нет',needs_input:'Нужно заполнить вручную',
                stale:'Исходные данные изменились',unknown_response:'Ответ модели неизвестен',
                failed:'Не удалось обработать',cancelled:'Отменено'})[status] || 'Предложений ещё нет'; },
            issueLabel(code) { return ({source_snapshot_missing:'Нет исходного снимка товара.',
                schema_stale:'Требования Ozon требуют обновления.',
                no_eligible_missing_attributes:'Для этой версии нет подходящих пустых характеристик.',
                unknown_response:'Ответ модели не подтверждён; проверьте карточку вручную.',
                ai_fields_rejected:'Проверка исходных сведений отклонила AI-предложения. Ничего не применено.',
                ai_suggestions_stale:'Источник, схема или заполненные поля изменились. Нужен новый запуск.'})[code] ||
                'Предложения требуют новой проверки карточки и исходных сведений.'; },
            itemOutcomeMessage() {
                const item = this.doc?.item;
                if (!item) return '';
                if (item.status === 'unknown_response')
                    return 'Ответ модели не подтверждён. Проверьте состояние карточки и историю запуска; автоматического повтора нет. При необходимости запустите новый поиск явно или заполните поля вручную.';
                if (item.code === 'ai_fields_rejected') {
                    return this.doc.suggestions?.length
                        ? 'Часть AI-предложений не прошла проверку исходных сведений и не сохранена. Проверьте доступные предложения; остальные поля заполните вручную или начните новый поиск после проверки источника.'
                        : 'Модель вернула предложения, но проверка источника их отклонила; ничего не сохранено. Проверьте источник, затем заполните поля вручную или запустите новый поиск явно.';
                }
                if (item.status === 'no_evidence')
                    return 'Модель не вернула подтверждённых предложений. Проверьте исходные сведения, затем заполните поля вручную или запустите новый поиск явно.';
                return '';
            },
            evidenceLabel(row) { return typeof row.path === 'string' ? row.path : ''; },
            selectedIds() { return this.available.filter(row => this.selected.includes(row.id)).map(row => row.id); },
            groupIds(row) {
                if (String(row.complex_id) === '0') return [row.id];
                return this.available.filter(item => String(item.complex_id) === String(row.complex_id) &&
                    item.group_ordinal === row.group_ordinal).map(item => item.id);
            },
            checked(row) { return this.selected.includes(row.id); },
            toggle(row, checked) {
                if (this.reviewLocked || !row.applicable) return;
                const group = this.groupIds(row), current = new Set(this.selected);
                for (const id of group) checked ? current.add(id) : current.delete(id);
                this.selected = [...current].sort((a,b) => a-b); this.confirmReview = false;
            },
            async request(url, options={}) {
                const abort = new AbortController(), timer = setTimeout(() => abort.abort(), options.method === 'POST' ? 45000 : 10000);
                try {
                    const response = await fetch(url, {method:options.method || 'GET', credentials:'same-origin',
                        redirect:'manual', cache:'no-store', signal:abort.signal,
                        headers:{Accept:'application/json', ...(options.method === 'POST' ?
                            {'Content-Type':'application/json','X-CSRFToken':this.localCsrf} : {}), ...(options.headers || {})},
                        ...(options.body ? {body:JSON.stringify(options.body)} : {})});
                    if (response.status === 401 || response.type === 'opaqueredirect' || response.redirected)
                        throw Object.assign(Error('Сессия завершилась. Войдите снова и прочитайте состояние.'), {status:401});
                    if (response.status === 403)
                        throw Object.assign(Error('Доступ к этой карточке больше недоступен.'), {status:403});
                    if (!(response.headers.get('content-type') || '').includes('application/json'))
                        throw Error('Ответ не подтверждён. Прочитайте сохранённое состояние.');
                    const body = await response.json();
                    if (typeof body.csrf_token === 'string' && body.csrf_token.length >= 20 &&
                        body.csrf_token.length <= 512) {
                        this.localCsrf = body.csrf_token;
                        this.$emit('csrf',body.csrf_token);
                    }
                    if (!response.ok || body.success !== true)
                        throw Object.assign(Error(body.error || 'Не удалось выполнить действие.'), {status:response.status});
                    return body;
                } finally { clearTimeout(timer); }
            },
            async loadSuggestions() {
                if (this.busy || this.sessionEnded) return;
                this.loading = true; this.error = '';
                try {
                    const item = new URLSearchParams(global.location.search).get('ai_item_id');
                    const query = item && /^[1-9]\d{0,18}$/.test(item) ? '?item_id='+item : '';
                    const body = await this.request(this.urls.suggestions+query);
                    if (body.draft_id !== this.draftId || body.account_id !== this.accountId ||
                        !positive(body.version) || !Array.isArray(body.suggestions) || body.suggestions.length > 200)
                        throw Error('Ответ относится к другой карточке. Действие остановлено.');
                    this.doc = body; this.selected = []; this.confirmReview = false;
                    this.$emit('suggestions',body.suggestions);
                } catch (error) { this.handle(error,'Не удалось прочитать предложения.'); }
                finally { this.loading = false; }
            },
            handle(error, fallback) {
                if (error.status === 401 || error.status === 403) this.sessionEnded = true;
                this.error = error.name === 'AbortError' ? 'Ответ занял слишком много времени. Повторите только чтение.' :
                    error instanceof TypeError ? 'Нет связи. Повторите только чтение.' : error.message || fallback;
                if (this.doc || this.busy) this.$nextTick(() => this.$refs.error?.focus());
            },
            openGenerate() {
                if (!this.canGenerate) return;
                this.confirmGenerate = false; this.$refs.generateDialog.showModal();
                this.$nextTick(() => this.$refs.generateCheck?.focus());
            },
            closeGenerate() {
                if (this.busy === 'generate') return;
                this.$refs.generateDialog?.close(); this.confirmGenerate = false; this.$refs.generateButton?.focus();
            },
            async startGenerate() {
                if (!this.canGenerate || !this.confirmGenerate) return;
                const pending = {account_id:this.accountId,draft_ids:[this.draftId],
                    expected_versions:{[String(this.draftId)]:this.version}, viewed_version:this.version,
                    request_key:key()};
                if (!save(this.generationStorage(),pending)) { this.error = 'Браузер не сохранил ключ запуска. Действие остановлено.'; return; }
                this.generationPending = pending;
                await this.submitGenerate(pending);
            },
            async retryGenerate() {
                if (!this.generationPending || !this.generationRetry || this.busy || this.sessionEnded) return;
                this.generationRetry = false; await this.submitGenerate(this.generationPending);
            },
            async submitGenerate(pending) {
                if (!this.validGeneration(pending) || this.busy || this.sessionEnded) return;
                this.busy = 'generate'; this.error = '';
                try {
                    const body = await this.request(this.urls.generate,{method:'POST',body:{
                        account_id:pending.account_id,draft_ids:pending.draft_ids,
                        expected_versions:pending.expected_versions,request_key:pending.request_key,confirm_generate:true}});
                    if (body.run?.account_id !== this.accountId || body.run?.mode !== 'draft_suggestions' ||
                        !uid.test(body.run.job_uid)) throw Error('Ответ AI-запуска не совпал с карточкой.');
                    forget(this.generationStorage()); this.generationPending = null;
                    global.location.assign(this.urls.runBase + body.run.job_uid);
                } catch (error) {
                    if (error.status && error.status < 500 && error.status !== 401 && error.status !== 403) {
                        forget(this.generationStorage()); this.generationPending = null;
                    }
                    this.handle(error,'Результат запуска неизвестен.');
                } finally { this.busy = ''; this.$refs.generateDialog?.close(); this.confirmGenerate = false; }
            },
            async readGenerate() {
                if (!this.generationPending || this.busy || this.sessionEnded) return;
                this.busy = 'read'; this.error = ''; this.generationRetry = false;
                try {
                    const body = await this.request(this.urls.byRequest+'?account_id='+this.accountId,
                        {headers:{'X-AI-Request-Key':this.generationPending.request_key}});
                    if (body.run?.account_id !== this.accountId || body.run?.mode !== 'draft_suggestions' ||
                        !uid.test(body.run.job_uid)) throw Error('Найденный запуск относится к другому магазину.');
                    forget(this.generationStorage()); this.generationPending = null;
                    global.location.assign(this.urls.runBase + body.run.job_uid);
                } catch (error) {
                    if (error.status === 404) this.generationRetry = true;
                    this.handle(error,'Не удалось найти запуск.');
                } finally { this.busy = ''; }
            },
            clearGenerationProblem() {
                if (!this.generationProblem || this.busy) return;
                forget(this.generationStorage()); this.generationProblem = false;
                this.notice = 'Повреждённая запись снята после проверки истории.';
            },
            clearReviewProblem() {
                if (!this.reviewProblem || this.busy) return;
                forget(this.reviewStorage()); this.reviewProblem = false;
                this.notice = 'Повреждённая запись решения снята после проверки черновика.';
            },
            openReview(action) {
                if (this.reviewLocked || !['apply','reject'].includes(action) || !this.selectedIds().length ||
                    this.doc.version !== this.version) return;
                this.reviewAction = action; this.confirmReview = false;
                this.$refs.reviewDialog.showModal(); this.$nextTick(() => this.$refs.reviewCheck?.focus());
            },
            closeReview() {
                if (this.busy === 'review') return;
                this.$refs.reviewDialog?.close(); this.confirmReview = false;
            },
            async decide() {
                if (this.reviewLocked || !this.confirmReview || !['apply','reject'].includes(this.reviewAction) ||
                    this.doc.version !== this.version) return;
                const pending = {account_id:this.accountId,draft_id:this.draftId,
                    action:this.reviewAction,suggestion_ids:this.selectedIds(),expected_version:this.doc.version,
                    review_token:this.doc.review_token,request_key:key()};
                if (!save(this.reviewStorage(),pending)) { this.error = 'Браузер не сохранил ключ решения. Действие остановлено.'; return; }
                this.reviewPending = pending;
                await this.submitReview(pending);
            },
            async readReview() {
                if (!this.reviewPending || this.busy || this.sessionEnded) return;
                await this.loadSuggestions();
                if (this.doc && !this.error) {
                    this.reviewCanReplay = true;
                    this.notice = 'Текущее состояние прочитано. Для подтверждения результата повторите ровно то же решение с сохранённым ключом.';
                }
            },
            async replayReview() {
                if (!this.reviewPending || !this.reviewCanReplay || this.busy || this.sessionEnded) return;
                this.reviewCanReplay = false; await this.submitReview(this.reviewPending);
            },
            async submitReview(pending) {
                if (!this.validReview(pending) || this.busy || this.sessionEnded) return;
                this.busy = 'review'; this.error = '';
                try {
                    const body = await this.request(this.urls[pending.action],{method:'POST',body:{
                        suggestion_ids:pending.suggestion_ids,expected_version:pending.expected_version,
                        review_token:pending.review_token,request_key:pending.request_key}});
                    const result = body.review;
                    if (!result || result.draft_id !== this.draftId || result.action !== pending.action ||
                        !same([...result.suggestion_ids].sort((a,b)=>a-b),[...pending.suggestion_ids].sort((a,b)=>a-b)))
                        throw Error('Ответ решения не совпал с выбранными предложениями.');
                    forget(this.reviewStorage()); this.reviewPending = null;
                    this.notice = pending.action === 'apply' ? 'Выбранные значения сохранены. Проверьте карточку заново перед отправкой.' :
                        'Выбранные предложения отклонены.';
                    if (pending.action === 'apply') this.$emit('applied');
                    this.busy = '';
                    await this.loadSuggestions();
                } catch (error) {
                    if (error.status && error.status < 500 && error.status !== 401 && error.status !== 403) {
                        forget(this.reviewStorage()); this.reviewPending = null;
                    }
                    this.handle(error,'Результат решения неизвестен.');
                } finally { this.busy = ''; this.$refs.reviewDialog?.close(); this.confirmReview = false; }
            },
        },
        watch: {csrf(value) { if (typeof value === 'string' && value.length >= 20 && value.length <= 512) this.localCsrf = value; }},
        mounted() { this.loadSuggestions(); },
        template: `<section class="ode-card ode-ai-review" aria-labelledby="ode-ai-review-title">
            <div class="ode-section-heading"><div><p class="ode-eyebrow">Необязательное дополнение</p><h2 id="ode-ai-review-title">Предложения AI</h2><p class="ode-muted">Только пропущенные характеристики с буквальным подтверждением в исходных сведениях. Ничего не применяется и не отправляется автоматически.</p></div><button ref="generateButton" type="button" class="sh-btn sh-btn--secondary" :disabled="!canGenerate" @click="openGenerate">Предложить недостающее</button></div>
            <p v-if="dirty" class="ode-help">Сохраните ручные изменения перед запуском или принятием AI-предложений.</p>
            <p v-if="!productTypeId" class="ode-help">Сначала выберите категорию и тип товара.</p>
            <p v-if="sessionEnded" class="ode-message ode-message--warning" role="status">Действия остановлены. Войдите снова и откройте карточку.</p>
            <p v-if="error" ref="error" class="ode-message ode-message--error" role="alert" tabindex="-1">{{ error }}</p>
            <p v-if="notice" class="ode-message ode-message--success" role="status">{{ notice }}</p>
            <div v-if="generationProblem || reviewProblem" class="ode-message ode-message--warning"><span>Сохранённое действие нельзя прочитать. Проверьте историю карточки перед новым действием.</span><button v-if="generationProblem" type="button" class="sh-btn sh-btn--secondary" @click="clearGenerationProblem">Я проверил AI-запуск</button><button v-if="reviewProblem" type="button" class="sh-btn sh-btn--secondary" @click="clearReviewProblem">Я проверил решение</button></div>
            <div v-if="generationPending" class="ode-message ode-message--warning"><span>Результат AI-запуска неизвестен. Сначала проверьте сохранённый ключ; повтор возможен только вручную с тем же запросом.</span><div class="ode-actions"><button type="button" class="sh-btn sh-btn--secondary" :disabled="!!busy || sessionEnded" @click="readGenerate">Проверить запуск</button><button v-if="generationRetry" type="button" class="sh-btn sh-btn--secondary" :disabled="!!busy || sessionEnded" @click="retryGenerate">Повторить тот же запрос</button></div></div>
            <div v-if="reviewPending" class="ode-message ode-message--warning"><span>Результат решения неизвестен. Прочитайте текущее состояние, затем явно повторите то же решение с сохранённым ключом.</span><div class="ode-actions"><button type="button" class="sh-btn sh-btn--secondary" :disabled="!!busy || sessionEnded" @click="readReview">Прочитать состояние</button><button v-if="reviewCanReplay" type="button" class="sh-btn sh-btn--secondary" :disabled="!!busy || sessionEnded" @click="replayReview">Повторить то же решение</button></div></div>
            <p v-if="loading" class="ode-help" role="status">Читаем предложения…</p><button v-if="!loading" type="button" class="ode-text-button" :disabled="!!busy || sessionEnded" @click="loadSuggestions">Обновить предложения</button>
            <p v-if="doc?.item" class="ode-help">Состояние AI-запуска: {{ statusLabel(doc.item.status) }} · версия черновика {{ doc.version }}. <a v-if="/^ozon-ai-[0-9a-f]{32}$/.test(doc.item.run_uid)" :href="urls.runBase+doc.item.run_uid" class="ode-link">Открыть запуск</a></p>
            <p v-if="doc?.code" class="ode-help">{{ issueLabel(doc.code) }}</p>
            <p v-if="itemOutcomeMessage()" class="ode-help ode-ai-outcome" role="status">{{ itemOutcomeMessage() }}</p>
            <p v-if="doc && !doc.suggestions.length && !itemOutcomeMessage()" class="ode-help">Для этой карточки пока нет подтверждённых предложений. Ручное заполнение доступно независимо.</p>
            <div v-if="doc?.suggestions?.length" class="ode-ai-list"><article v-for="row in doc.suggestions" :key="row.id" class="ode-ai-item" :class="{'is-accepted':row.status === 'accepted','is-proposed':row.status === 'proposed'}"><label v-if="row.applicable" class="ode-ai-check"><input type="checkbox" :checked="checked(row)" :disabled="reviewLocked" :aria-label="'Выбрать предложение ' + row.name" @change="toggle(row,$event.target.checked)"></label><span v-else class="ode-ai-check-spacer"></span><div><div class="ode-ai-item-head"><strong>{{ row.name }}</strong><span>{{ row.status === 'accepted' ? 'Принято вами' : row.status === 'rejected' ? 'Отклонено' : row.applicable ? 'Предложено AI' : 'Нужна новая проверка' }}</span></div><p>{{ row.status === 'accepted' ? 'Принятое значение' : 'Предложенное значение' }}: <strong>{{ row.label || 'значение не указано' }}</strong></p><small v-if="String(row.complex_id) !== '0'">Связанная группа {{ row.group_ordinal }}: выбор применяется ко всей группе.</small><details v-if="row.evidence?.length"><summary>Подтверждение из исходных сведений</summary><ul><li v-for="(fact,index) in row.evidence" :key="index"><span>{{ evidenceLabel(fact) }}</span><q>{{ fact.quote }}</q></li></ul></details><p v-else class="ode-help">Нет буквального подтверждения; значение не должно применяться.</p></div></article></div>
            <div v-if="selectedIds().length" class="ode-ai-actions"><span>Выбрано значений: {{ selectedIds().length }}</span><button type="button" class="sh-btn sh-btn--secondary" :disabled="reviewLocked" @click="openReview('reject')">Отклонить выбранные</button><button type="button" class="sh-btn sh-btn--primary" :disabled="reviewLocked" @click="openReview('apply')">Принять выбранные</button></div>
            <dialog ref="generateDialog" class="ode-dialog ode-ai-dialog" aria-labelledby="ode-ai-generate-title" @cancel.prevent="closeGenerate"><h2 id="ode-ai-generate-title">Запустить AI-дополнение?</h2><p>Черновик № {{ draftId }} · сохранённая версия {{ version }}. Модель получит только разрешённые исходные факты и предложит значения для пустых характеристик. Отправки в Ozon и автоматического изменения карточки не будет.</p><label class="ode-checkbox"><input ref="generateCheck" type="checkbox" v-model="confirmGenerate">Я хочу получить предложения для этой версии</label><div class="ode-actions"><button type="button" class="sh-btn sh-btn--secondary" :disabled="busy === 'generate'" @click="closeGenerate">Вернуться</button><button type="button" class="sh-btn sh-btn--primary" :disabled="!confirmGenerate || !canGenerate" @click="startGenerate">Запустить предложения</button></div></dialog>
            <dialog ref="reviewDialog" class="ode-dialog ode-ai-dialog" aria-labelledby="ode-ai-decision-title" @cancel.prevent="closeReview"><h2 id="ode-ai-decision-title">{{ reviewAction === 'apply' ? 'Принять' : 'Отклонить' }} {{ selectedIds().length }} предложений?</h2><p>Решение относится только к этим характеристикам и сохранённой версии {{ doc?.version }}. Принятие сохранит значения в черновике и потребует новой проверки карточки; Ozon не меняется.</p><label class="ode-checkbox"><input ref="reviewCheck" type="checkbox" v-model="confirmReview">Я проверил предложения и буквальные подтверждения источника</label><div class="ode-actions"><button type="button" class="sh-btn sh-btn--secondary" :disabled="busy === 'review'" @click="closeReview">Вернуться</button><button type="button" class="sh-btn sh-btn--primary" :disabled="!confirmReview || reviewLocked" @click="decide">Подтвердить решение</button></div></dialog>
        </section>`,
    };
    global.ozonDraftAIReview = {component};
})(window);
