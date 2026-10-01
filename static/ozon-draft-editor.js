/* Vue editor: local draft mutations, explicit publication, exact cached dictionaries. */
(function (global) {
    'use strict';
    const clone = value => JSON.parse(JSON.stringify(value));
    const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const identity = item => String(item.attribute_id) + ':' + String(item.complex_id || '0');
    const empty = value => value === '' || value === null || value === undefined;
    const decimal = value => typeof value === 'string' && /^\d+,\d+$/.test(value.trim()) ? value.trim().replace(',', '.') : value;
    function cleanObject(value) { return Object.fromEntries(Object.entries(value || {}).filter(([, v]) => !empty(v))); }
    function attributes(rows) {
        return (rows || []).map(row => ({...row, values: (row.values || []).filter(v => !empty(v.value))})).filter(row => row.values.length);
    }
    function serialize(form) {
        const dimensions = cleanObject(form.dimensions), commercial = cleanObject(form.commercial);
        for (const key of ['width', 'height', 'depth', 'weight']) if (key in dimensions) dimensions[key] = decimal(dimensions[key]);
        for (const key of ['price', 'old_price']) if (key in commercial) commercial[key] = decimal(commercial[key]);
        return {
            offer_id: form.offer_id,
            content: clone(form.content),
            attributes: attributes(clone(form.attributes)),
            complex_attributes: (form.complex_attributes || []).map(group => ({attributes: attributes(clone(group.attributes))})).filter(group => group.attributes.length),
            attribute_removals: clone(form.attribute_removals),
            media: {...cleanObject(form.media), images: (form.media.images || []).filter(Boolean)},
            dimensions,
            commercial,
            barcodes: (form.barcodes || []).filter(Boolean),
        };
    }
    function safeImage(url) {
        if (typeof url !== 'string' || url.length > 2000) return '';
        try { const u = new URL(url); return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password ? url : ''; } catch (_) { return ''; }
    }
    const photoComponent = {
        directives: {'image-deadline':global.mcatShared?.imageDeadline || {}},
        props: ['src', 'alt', 'compact', 'retryableCompact', 'sourceId'],
        data() { return {attempt:0, manualCycle:0, state:'idle', timer:null}; },
        computed: {
            exactSource() {
                const match = typeof this.src === 'string' && this.src.match(/^\/api\/photos\/imported-product\/([1-9]\d*)\/(0|[1-9]\d*)\?deferred=1$/);
                return !!match && String(this.sourceId) === match[1];
            },
            usable() { return this.exactSource || !!safeImage(this.src); },
            url() {
                if (!this.exactSource) return this.src;
                return this.src + (this.attempt ? '&preview_attempt=' + this.attempt : '') +
                    (this.manualCycle ? '&manual_retry=' + this.manualCycle : '');
            },
        },
        methods: {
            stopTimer() { clearTimeout(this.timer); this.timer = null; },
            reset() {
                this.stopTimer(); this.attempt = 0; this.manualCycle = 0;
                this.state = !this.usable ? 'missing' : document.hidden ? 'paused' : 'loading';
            },
            onVisibility() {
                if (document.hidden && (this.state === 'loading' || this.state === 'pending')) {
                    this.stopTimer(); this.state = 'paused';
                }
            },
            onLoad(event) {
                if (this.state !== 'loading' || event.target !== this.$refs.photo ||
                    event.target.getAttribute('src') !== this.url ||
                    event.target.currentSrc !== new URL(this.url, location.href).href ||
                    !event.target.naturalWidth) return;
                this.stopTimer(); this.state = 'ready';
            },
            onError(event) {
                if (this.state !== 'loading' || event.target !== this.$refs.photo ||
                    event.target.dataset.expectedUrl !== this.url ||
                    (event.target.getAttribute('src') && event.target.getAttribute('src') !== this.url)) return;
                this.stopTimer();
                if (document.hidden) { this.state = 'paused'; return; }
                if (!this.exactSource || this.attempt >= 3) { this.state = 'failed'; return; }
                this.state = 'pending';
                this.timer = setTimeout(() => {
                    this.timer = null;
                    if (document.hidden) { this.state = 'paused'; return; }
                    this.attempt += 1; this.state = 'loading';
                }, 2000 * (this.attempt + 1));
            },
            retry() {
                if (document.hidden || (this.state !== 'failed' && this.state !== 'paused')) return;
                this.stopTimer(); this.manualCycle += 1; this.attempt = 0; this.state = 'loading';
            },
        },
        watch: {src() { this.reset(); }, sourceId() { this.reset(); }},
        mounted() { this.reset(); document.addEventListener('visibilitychange', this.onVisibility); },
        beforeUnmount() { this.stopTimer(); document.removeEventListener('visibilitychange', this.onVisibility); },
        template: `<span class="ode-image-frame"><img v-image-deadline v-if="usable && (state === 'loading' || state === 'ready')" :key="url + ':' + manualCycle" ref="photo" :src="url" :data-expected-url="url" :alt="alt || ''" width="320" height="320" loading="lazy" decoding="async" referrerpolicy="no-referrer" @load="onLoad" @error="onError"><span v-if="state === 'loading' || state === 'pending'" class="ode-image-state" :class="{'is-compact':compact}" role="status">{{ compact ? 'Ждём…' : 'Загружаем фото…' }}</span><span v-else-if="state === 'missing'" class="ode-image-state" :class="{'is-compact':compact}">{{ compact ? 'Нет фото' : 'Фото не найдено' }}</span><span v-else-if="state === 'paused'" class="ode-image-state" :class="{'is-compact':compact}" role="status">{{ compact ? 'Пауза' : 'Загрузка остановлена' }}<button v-if="!compact || retryableCompact" type="button" class="ode-text-button" :aria-label="'Продолжить загрузку фото ' + (alt || 'товара')" @click.stop="retry">{{ compact ? '↻' : 'Продолжить' }}</button></span><span v-else-if="state === 'failed'" class="ode-image-state" :class="{'is-compact':compact}" role="status">{{ compact ? 'Нет фото' : 'Фото недоступно' }}<button v-if="!compact || retryableCompact" type="button" class="ode-text-button" :aria-label="'Повторить загрузку фото ' + (alt || 'товара')" @click.stop="retry">{{ compact ? '↻' : 'Повторить' }}</button></span></span>`,
    };
    const fieldComponent = {
        props: ['definition', 'values', 'locked', 'aiStatus'], emits: ['change', 'dictionary', 'remove'],
        methods: {
            change(index, value) { const next = clone(this.values); next[index] = {value:/^decimal$/i.test(this.definition.data_type) ? decimal(value) : value}; this.$emit('change', next); },
            remove(index) { const next = clone(this.values); next.splice(index, 1); this.$emit(next.length ? 'change' : 'remove', next); },
            add() { this.$emit('change', [...clone(this.values), {value: ''}]); },
        },
        template: `<div class="ode-attribute" :data-attribute="definition.id" :class="{'ode-attribute--ai':aiStatus === 'proposed'}">
            <div class="ode-field-heading"><span>{{ definition.name }} <span v-if="definition.required" class="ode-required" aria-label="Обязательное поле">*</span></span><small v-if="aiStatus === 'proposed'" class="ode-ai-field-badge">Предложено AI · проверьте ниже</small><small v-else-if="aiStatus === 'accepted'" class="ode-ai-field-badge">Принято вами</small><small v-else-if="!definition.editable">Недоступно для изменения</small></div>
            <p v-if="definition.description" class="ode-help">{{ definition.description }}</p>
            <template v-if="definition.dictionary">
                <div class="ode-value-chips"><span v-for="(value, index) in values" :key="index" class="ode-value-chip">{{ value.value }}<button v-if="!locked" type="button" @click="remove(index)" :aria-label="'Убрать значение ' + value.value">×</button></span><span v-if="!values.length" class="ode-muted">Значение не выбрано</span></div>
                <button type="button" class="sh-btn sh-btn--secondary sh-btn--sm" :disabled="locked || !definition.dictionary_fresh" @click="$emit('dictionary')">Выбрать из справочника</button>
                <p v-if="!definition.dictionary_fresh" class="ode-help">Справочник требует обновления или проверки администратором. Сохранённые значения остаются в карточке.</p>
            </template>
            <template v-else>
                <div v-for="(value, index) in (values.length ? values : [{value: ''}])" :key="index" class="ode-value-input">
                    <select v-if="/^boolean$/i.test(definition.data_type)" class="sh-input" :aria-label="definition.name" :aria-required="definition.required ? 'true' : undefined" :value="value.value" :disabled="locked" @change="change(index, $event.target.value)"><option value="">Не выбрано</option><option value="true">Да</option><option value="false">Нет</option><option v-if="value.value && !['true','false'].includes(value.value)" :value="value.value">{{ value.value }} · требует проверки</option></select>
                    <input v-else class="sh-input" :aria-label="definition.name + (index ? ' — значение ' + (index + 1) : '')" :aria-required="definition.required ? 'true' : undefined" :value="value.value" :disabled="locked" maxlength="1000" :inputmode="/Integer|Decimal|Number/i.test(definition.data_type) ? 'decimal' : 'text'" @input="change(index, $event.target.value)">
                    <button v-if="values.length > 1 && !locked" type="button" class="ode-icon-button" @click="remove(index)" :aria-label="'Убрать значение ' + (index + 1)">×</button>
                </div>
                <button v-if="definition.collection && values.length < definition.max_values" type="button" class="ode-text-button" :disabled="locked" @click="add">+ Ещё значение</button>
            </template>
            <button v-if="values.length && !locked" type="button" class="ode-text-button ode-danger" @click="$emit('remove')">Очистить характеристику</button>
        </div>`,
    };
    function createOptions(config) {
        let loadController, searchController, searchTimer, searchRevision = 0, typeController, typeTimer, typeRevision = 0;
        let alive = true, beforeUnload, dialogReturnFocus, categoryController;
        return {
            components: {'attribute-field': fieldComponent, 'draft-photo':photoComponent,
                'ai-suggestions': global.ozonDraftAIReview?.component},
            data() { return {
                config, loading: true, busy: '', error: '', notice: '', conflict: false, uncertain: false,
                data: null, draft: null, form: null, original: null, section: 'content', attrQuery: '', showOptional: false, attrLimit: 40, aiRows: [],
                failedImages: [], photoIndex: 0, newPhoto: '', typeQuery: '', typeOptions: [], typeLoading: false, typeError: '', selectedType: null, saveMapping: false,
                dictionary: {open: false, definition: null, groupIndex: null, query: '', items: [], loading: false, error: '', more: false},
                categoryReview: {open:false, loading:false, saving:false, rows:[], total:0, page:0, hasMore:false, digest:'', token:'', target:null, initialType:null, mapping:false, viewedVersion:null, confirmed:false, error:'', pending:null, needsChoice:false},
                confirmation: null, confirmedWrite: false,
                tabs: [{id:'content', label:'Основное'}, {id:'media', label:'Фотографии'}, {id:'attributes', label:'Характеристики'}, {id:'delivery', label:'Цена и упаковка'}],
            }; },
            computed: {
                dirty() { return !!this.form && !equal(serialize(this.form), this.original); },
                locked() { return !config.enabled || !!this.busy || this.conflict || this.uncertain || !this.draft || this.draft.status === 'archived' || !!this.data.active_operation_id; },
                schemaFresh() { return !!this.data?.readiness?.schema?.fresh; },
                currentValidationAvailable() {
                    const result = this.data?.current_validation;
                    return !!result && typeof result === 'object' &&
                        typeof result.publishable === 'boolean' && Array.isArray(result.errors);
                },
                canPublish() { return !this.locked && !this.dirty && !this.data?.write_quarantine && config.publicationEnabled && this.currentValidationAvailable && this.data.current_validation.publishable && this.draft.status === 'ready' && this.draft.validation_status === 'valid' && this.data.readiness.overall === 'ready' && !this.data.baseline_error; },
                quarantineUrl() { const url = this.data?.write_quarantine?.review_url; return typeof url === 'string' && /^\/marketplaces\/operations\/[1-9]\d*\/review$/.test(url) ? url : null; },
                errors() { return !this.dirty && this.currentValidationAvailable && Array.isArray(this.data.current_validation.errors) ? this.data.current_validation.errors : []; },
                warnings() { return !this.dirty && this.currentValidationAvailable && Array.isArray(this.data.current_validation.warnings) ? this.data.current_validation.warnings : []; },
                photos() { return this.form ? [this.form.media.primary_image, ...(this.form.media.images || [])].filter((url, i, all) => safeImage(url) && all.indexOf(url) === i) : []; },
                preservedPhotos() { const media = this.data?.preserved_media || {}; return [media.primary_image, ...(media.images || [])].filter(Boolean); },
                hero() { const photo = this.photos[this.photoIndex] || this.photos[0]; return this.failedImages.includes(photo) ? '' : photo; },
                previewPrice() {
                    const value = String(decimal(this.form.commercial.price) || '').trim();
                    if (!/^\d+(\.\d+)?$/.test(value) || !Number.isFinite(Number(value)) || Number(value) > 1e15) return 'Цена не указана';
                    return new Intl.NumberFormat('ru-RU', {maximumFractionDigits:2}).format(Number(value)) + (this.form.commercial.currency_code === 'RUB' ? ' ₽' : '');
                },
                simpleDefinitions() {
                    const known = this.data.definitions.filter(def => def.complex_id === '0' && def.id !== '4191');
                    const ids = new Set(known.map(def => def.id));
                    for (const row of this.form.attributes) if (row.attribute_id !== '4191' && !ids.has(row.attribute_id)) {
                        known.push({id:row.attribute_id, complex_id:row.complex_id || '0', name:'Характеристика ' + row.attribute_id, editable:false, max_values:100}); ids.add(row.attribute_id);
                    }
                    return known;
                },
                filteredDefinitions() {
                    const query = this.attrQuery.trim().toLocaleLowerCase('ru');
                    return this.simpleDefinitions.filter(def => (!query ? this.showOptional || def.required || this.valuesFor(def).length || !!this.aiStatus(def) : /^\d+$/.test(query) ? def.id === query : (def.name + ' ' + def.id).toLocaleLowerCase('ru').includes(query)));
                },
                visibleDefinitions() { return this.filteredDefinitions.slice(0, this.attrLimit); },
                complexTypes() {
                    const result = new Map();
                    for (const def of this.data.definitions) if (def.complex_id !== '0' && !result.has(def.complex_id)) result.set(def.complex_id, {id:def.complex_id, name:def.group || 'Группа ' + def.complex_id, collection:def.complex_collection});
                    return [...result.values()];
                },
                activeOperationNotice() {
                    if (!this.data?.active_operation_id) return null;
                    const operation = this.data.operations?.find(item => item.id === this.data.active_operation_id);
                    const status = operation?.status;
                    if (status === 'uncertain') return {
                        label:'Нужна сверка',
                        description:operation.next_poll_at
                            ? 'Результат отправки пока не подтверждён. Проверка запланирована; редактирование недоступно до выяснения результата.'
                            : 'Результат отправки не подтверждён. Автоматическая проверка остановлена. Откройте операцию для сверки; повторная отправка заблокирована.',
                    };
                    return {
                        queued:{label:'В очереди на отправку', description:'Карточка ожидает отправки в Ozon. Пока операция активна, редактирование недоступно.'},
                        submitting:{label:'Отправка выполняется', description:'Отправляем карточку в Ozon. Результат ещё не подтверждён; редактирование пока недоступно.'},
                        submitted:{label:'Проверяем результат отправки', description:'Ozon принял задачу. Проверяем результат; редактирование пока недоступно.'},
                        polling:{label:'Проверяем результат отправки', description:'Проверяем результат отправки в Ozon. Редактирование недоступно до выяснения результата.'},
                    }[status] || {label:'Нужно проверить состояние отправки', description:'Состояние операции не подтверждено. Откройте её подробности; редактирование и повторная отправка пока недоступны.'};
                },
                statusLabel() {
                    if (this.dirty) return 'Есть несохранённые изменения';
                    if (this.activeOperationNotice) return this.activeOperationNotice.label;
                    if (this.draft?.status === 'published') return 'Опубликован';
                    if (this.draft?.status === 'archived') return 'Архив';
                    if (!this.currentValidationAvailable) return 'Текущая проверка не подтверждена';
                    if (!this.data.current_validation.publishable) {
                        const errors = this.errors;
                        const packageMissing = errors.some(issue =>
                            ['physical_fact_required', 'dimension_unit_required', 'weight_unit_required'].includes(issue.code) &&
                            String(issue.field || '').startsWith('dimensions.')
                        );
                        const vatMissing = errors.some(issue => issue.code === 'vat_required');
                        if (packageMissing && vatMissing) return 'Заполните упаковку и выберите ставку НДС';
                        if (packageMissing) return 'Укажите габариты и вес упаковки';
                        if (vatMissing) return 'Выберите ставку НДС';
                    }
                    const readinessLabel = {source_stale:'Обновите исходные сведения',references_stale:'Требования Ozon обновляются',needs_attributes:'Заполните обязательные поля',account_blocked:'Проверьте подключение магазина'}[this.data?.readiness?.overall];
                    if (readinessLabel && this.draft?.status !== 'archived') return readinessLabel;
                    if (!this.data.current_validation.publishable) return 'Исправьте замечания текущей проверки';
                    if (this.draft.status !== 'ready' || this.draft.validation_status !== 'valid') return 'Сохраните результат текущей проверки';
                    return {needs_category:'Нужна категория', draft:'На подготовке', ready:'Текущая проверка пройдена', published:'Опубликован', blocked:'Нужны исправления', archived:'Архив'}[this.draft?.status] || 'Черновик';
                },
                categoryLabel() { return [this.draft?.category_path, this.draft?.product_type_name].filter(Boolean).join(' / ') || 'Выберите категорию Ozon'; },
                linkedCategory() { return this.data?.linked_category || {mode:this.draft?.published_listing_id ? 'unavailable' : 'new'}; },
            },
            methods: {
                validationDate(value) {
                    if (typeof value !== 'string') return '';
                    const match = value.match(/^(\d{4})-(\d{2})-(\d{2})/);
                    return match ? match[3] + '.' + match[2] + '.' + match[1] : '';
                },
                safeImage,
                adoptAiCsrf(value) { if (typeof value === 'string' && value.length >= 20 && value.length <= 512) config.csrf = value; },
                receiveAiSuggestions(rows) { this.aiRows = Array.isArray(rows) ? rows : []; },
                aiStatus(def) {
                    if (!this.draft || this.dirty) return '';
                    const row = this.aiRows.find(item => String(item.attribute_id) === String(def.id) &&
                        String(item.complex_id || '0') === String(def.complex_id || '0') &&
                        (item.status === 'proposed' || item.status === 'accepted'));
                    return row?.status || '';
                },
                photoPreview(url) { return this.data?.photo_previews?.[url] || safeImage(url); },
                operationLabel(status) { return {queued:'В очереди',submitting:'Отправляется',submitted:'Отправлено',polling:'Проверка результата',uncertain:'Нужна сверка',succeeded:'Выполнено',partial:'Частично выполнено',failed:'Ошибка',cancelled:'Отменено'}[status] || 'Проверка состояния'; },
                operationUrl(id) { return config.urls.operations + id; },
                async request(url, options = {}) {
                    const controller = new AbortController(); let timedOut = false;
                    const abort = () => controller.abort();
                    options.signal?.addEventListener('abort', abort, {once:true});
                    if (options.signal?.aborted) controller.abort();
                    const timeoutMs = options.timeoutMs || 25000;
                    const fetchOptions = {...options}; delete fetchOptions.timeoutMs;
                    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
                    try {
                        const response = await fetch(url, {...fetchOptions, signal:controller.signal, credentials:'same-origin', headers:{Accept:'application/json', ...(options.body ? {'Content-Type':'application/json', 'X-CSRFToken':config.csrf} : {}), ...(options.headers || {})}});
                        if (response.status === 401 || response.redirected) throw Object.assign(Error('Сессия истекла. Войдите в Seller Hub в другой вкладке, чтобы не потерять правки.'), {status:401});
                        if (!(response.headers.get('content-type') || '').includes('application/json')) throw Error('Сервер вернул неожиданный ответ. Правки остаются на экране.');
                        const body = await response.json();
                        if (!response.ok || body.success === false) throw Object.assign(Error(body.error || 'Не удалось выполнить действие.'), {status:response.status});
                        if (url === config.urls.editor && (!options.method || options.method === 'GET')) {
                            if (body.draft?.id !== config.draftId || body.draft?.account_id !== config.accountId) {
                                throw Error('Сервер вернул данные другого черновика. Действие остановлено.');
                            }
                            if (typeof body.csrf === 'string' && body.csrf.length >= 20 && body.csrf.length <= 512) config.csrf = body.csrf;
                        }
                        return body;
                    } catch (error) {
                        if (timedOut) throw Error('Сервер не ответил вовремя. Данные остаются на экране; результат действия можно проверить загрузкой сохранённой версии.');
                        throw error;
                    } finally { clearTimeout(timeout); options.signal?.removeEventListener('abort', abort); }
                },
                hydrate(data) {
                    this.closeDictionary(); this.cancelTypeSearch(); this.data = data; this.draft = data.draft;
                    this.form = {...clone(data.documents), offer_id:data.draft.offer_id, attribute_removals:clone(data.draft.attribute_removals || [])};
                    this.form.media.images ||= []; this.form.content.name ||= ''; this.form.content.description ||= '';
                    this.original = serialize(this.form); this.typeOptions = this.initialTypeOptions(); this.typeQuery = ''; this.selectedType = null;
                    this.conflict = false; this.uncertain = false; this.photoIndex = 0;
                },
                async load(confirmDiscard = false) {
                    if (this.busy) return;
                    if (confirmDiscard && this.dirty && !global.confirm('Загрузить сохранённую версию? Несохранённые правки в этой вкладке будут потеряны.')) return;
                    loadController?.abort(); loadController = new AbortController(); const current = loadController;
                    this.loading = true; this.error = '';
                    try { const result = await this.request(config.urls.editor, {signal:current.signal}); if (alive && loadController === current) this.hydrate(result); }
                    catch (error) { if (alive && error.name !== 'AbortError' && loadController === current) this.error = this.readableError(error); }
                    finally { if (alive && loadController === current) this.loading = false; }
                },
                readableError(error) { return error instanceof TypeError ? 'Нет ответа от сервера. Проверьте соединение; введённые данные остаются на экране.' : error.message; },
                patch() { const current = serialize(this.form); return Object.fromEntries(Object.entries(current).filter(([key, value]) => !equal(value, this.original[key]))); },
                async mutate(action, body, message) {
                    if (this.locked || this.loading) return false;
                    this.busy = action; this.error = ''; this.notice = '';
                    try {
                        await this.request(config.urls[action], {method:'POST', body:JSON.stringify(body)});
                        // A successful write followed by a failed read must never enable a repeat write.
                        this.uncertain = true;
                        const result = await this.request(config.urls.editor);
                        if (!alive) return false;
                        this.hydrate(result); this.notice = message; return true;
                    } catch (error) {
                        if (!alive) return false;
                        this.conflict = error.status === 409 || error.status === 403 || error.status === 401;
                        if (!error.status || error.status >= 500) this.uncertain = true;
                        this.error = this.readableError(error); return false;
                    } finally { if (alive) this.busy = ''; }
                },
                async save() {
                    if (!this.dirty) return;
                    const selected = this.selectedType, query = this.typeQuery, mapping = this.saveMapping;
                    if (await this.mutate('save', {expected_version:this.draft.version, patch:this.patch()}, 'Изменения сохранены. Теперь проверьте карточку перед отправкой.')) {
                        this.selectedType = selected; this.typeQuery = query; this.saveMapping = mapping;
                        if (selected && !this.typeOptions.some(item => item.id === selected.id)) this.typeOptions = [selected, ...this.typeOptions];
                    }
                },
                async validate() { if (this.dirty) return; await this.mutate('validate', {expected_version:this.draft.version}, 'Проверка завершена. Результат показан в блоке готовности.'); },
                async refreshFacts() {
                    if (this.dirty || !global.confirm('Обновить исходные сведения о товаре? Сохранённые пользовательские поля останутся без изменений.')) return;
                    await this.mutate('refresh', {expected_version:this.draft.version}, 'Исходные сведения обновлены. Проверьте карточку ещё раз.');
                },
                setSection(section) { this.section = section; },
                initialTypeOptions() {
                    // Path-only lexical matches (25/30) are too broad to present
                    // as a suggestion. Explicit search still lists every match.
                    return (this.data?.suggestions || []).filter(item => Number.isFinite(item.score) && item.score >= 55);
                },
                issueLabel(issue) {
                    const labels = {
                        product_type_required:'Выберите категорию и тип товара Ozon.',
                        product_type_unavailable:'Выбранный тип товара недоступен. Выберите другой в справочнике.',
                        offer_id_required:'Укажите артикул продавца.',
                        offer_id_too_long:'Артикул слишком длинный. Сократите его до 50 символов.',
                        offer_id_already_published:'Этот артикул уже занят другой карточкой в магазине.',
                        dimension_unit_required:'Выберите единицу измерения габаритов упаковки.',
                        weight_unit_required:'Выберите единицу измерения веса с упаковкой.',
                        price_required:'Укажите рассчитанную цену продажи больше нуля.',
                        old_price_not_greater:'Цена до скидки должна быть выше цены продажи.',
                        old_price_invalid:'Цена до скидки должна быть числом больше нуля.',
                        vat_required:'Выберите ставку НДС для этого товара.',
                        currency_code_required:'Выберите валюту «Российский рубль».',
                        barcodes_limit:'Для отправки карточки оставьте один штрихкод.',
                        description_attribute_unavailable:'Требования Ozon к описанию пока недоступны. Дождитесь обновления справочника и повторите проверку.',
                        description_attribute_unsupported:'Формат описания в этой категории пока не поддерживается. Обратитесь в поддержку.',
                        description_attribute_duplicated:'Описание продублировано в характеристиках. Оставьте одно значение.',
                        description_attribute_conflict:'Описание в характеристиках отличается от основного описания. Приведите их к одному тексту.',
                        images360_removed:'Ozon больше не принимает фотографии 360° в этом способе отправки. Удалите их из карточки.'
                    };
                    const physical = {'dimensions.width':'Ширина упаковки', 'dimensions.height':'Высота упаковки', 'dimensions.depth':'Длина упаковки', 'dimensions.weight':'Вес с упаковкой'};
                    if (physical[issue.field] && issue.code === 'physical_fact_required') return physical[issue.field] + ': укажите подтверждённое значение больше нуля.';
                    if (physical[issue.field] && issue.code === 'physical_fact_not_integer') return physical[issue.field] + ': в выбранной единице нужно целое число.';
                    return labels[issue.code] || issue.message;
                },
                goToIssue(issue) {
                    const field = issue.field || '';
                    this.section = /media|image/.test(field) ? 'media' : /commercial|price|dimension|weight|barcode|vat/.test(field) ? 'delivery' : /attribute/.test(field) ? 'attributes' : 'content';
                    const indexedField = field.match(/^attributes\[(\d+)\](?:\.|$)/);
                    const indexedRow = indexedField ? this.form?.attributes?.[Number(indexedField[1])] : null;
                    const attribute = String(issue.attribute_id || field.match(/^attributes\.(\d+)/)?.[1] || indexedRow?.attribute_id || '');
                    if (attribute === '4191') this.section = 'content';
                    else if (attribute) this.attrQuery = attribute;
                    const ids = {'content.name':'ode-name', 'content.description':'ode-description', 'offer_id':'ode-offer', 'commercial.price':'ode-price', 'commercial.old_price':'ode-old-price', 'commercial.vat':'ode-vat', 'commercial.currency_code':'ode-currency', 'dimensions.width':'ode-width', 'dimensions.height':'ode-height', 'dimensions.depth':'ode-depth', 'dimensions.weight':'ode-weight', 'dimensions.dimension_unit':'ode-dimension-unit', 'dimensions.weight_unit':'ode-weight-unit', 'attributes.4191':'ode-description'};
                    this.$nextTick(() => {
                        const fieldElement = attribute && attribute !== '4191' ? [...document.querySelectorAll('.ode-attribute')].find(element => element.dataset.attribute === attribute) : null;
                        if (fieldElement?.closest('details')) fieldElement.closest('details').open = true;
                        const target = fieldElement || document.getElementById(ids[field] || (attribute ? ids['attributes.' + attribute] : '') || (/type|category/.test(field) ? 'ode-category' : 'ode-section'));
                        target?.scrollIntoView({behavior:'auto', block:'center'}); (fieldElement?.querySelector('input:not(:disabled), button:not(:disabled)') || target)?.focus();
                    });
                },
                valuesFor(def, groupIndex = null) { const rows = groupIndex === null ? this.form.attributes : this.form.complex_attributes[groupIndex].attributes; return rows.find(row => identity(row) === def.id + ':' + def.complex_id)?.values || []; },
                setValues(def, values, groupIndex = null) {
                    const rows = groupIndex === null ? this.form.attributes : this.form.complex_attributes[groupIndex].attributes;
                    const index = rows.findIndex(row => identity(row) === def.id + ':' + def.complex_id);
                    const item = {attribute_id:def.id, complex_id:def.complex_id, values:clone(values)};
                    if (index < 0) rows.push(item); else rows.splice(index, 1, item);
                    if (values.some(value => !empty(value.value))) this.form.attribute_removals = this.form.attribute_removals.filter(row => identity(row) !== identity(item));
                },
                removeAttribute(def, groupIndex = null) {
                    const key = def.id + ':' + def.complex_id;
                    const live = this.data.baseline_attribute_identities.some(row => identity(row) === key);
                    if (live && !global.confirm('Удалить «' + def.name + '» из карточки Ozon при следующей отправке? Для составной характеристики удалятся все её повторения.')) return;
                    this.setValues(def, [], groupIndex);
                    if (live) {
                        this.form.attributes = this.form.attributes.filter(row => identity(row) !== key);
                        this.form.complex_attributes.forEach(group => { group.attributes = group.attributes.filter(row => identity(row) !== key); });
                        if (!this.form.attribute_removals.some(row => identity(row) === key)) this.form.attribute_removals.push({attribute_id:def.id, complex_id:def.complex_id});
                    }
                },
                removalLabel(item) { return this.data.definitions.find(def => def.id === item.attribute_id && def.complex_id === (item.complex_id || '0'))?.name || 'Характеристика ' + item.attribute_id; },
                cancelRemoval(item) {
                    if (this.locked) return;
                    this.form.attribute_removals = this.form.attribute_removals.filter(row => identity(row) !== identity(item));
                    const original = this.data.documents.attributes.find(row => identity(row) === identity(item));
                    if (original && !this.form.attributes.some(row => identity(row) === identity(item))) this.form.attributes.push(clone(original));
                    this.notice = 'Удаление отменено. После сохранения значение из Ozon снова будет показано в карточке.';
                },
                groupDefinitions(group) {
                    const ids = new Set(group.attributes.map(row => row.complex_id));
                    const definitions = this.data.definitions.filter(def => ids.has(def.complex_id));
                    for (const row of group.attributes) if (!definitions.some(def => identity(row) === def.id + ':' + def.complex_id)) definitions.push({id:row.attribute_id,complex_id:row.complex_id,name:'Характеристика ' + row.attribute_id,editable:false});
                    return definitions;
                },
                addGroup(type) {
                    if (this.locked || !this.schemaFresh || this.form.complex_attributes.length >= 500) return;
                    if (!type.collection && this.form.complex_attributes.some(group => group.attributes.some(row => row.complex_id === type.id))) return;
                    this.form.complex_attributes.push({attributes:this.data.definitions.filter(def => def.complex_id === type.id).map(def => ({attribute_id:def.id,complex_id:def.complex_id,values:[]}))});
                },
                addPhoto() {
                    if (!safeImage(this.newPhoto)) { this.error = 'Укажите полную ссылку на изображение, начинающуюся с https:// или http://.'; return; }
                    if (this.photos.includes(this.newPhoto)) { this.error = 'Это фото уже есть в карточке.'; return; }
                    if (this.photos.length >= 30) return;
                    if (!this.form.media.primary_image) this.form.media.primary_image = this.newPhoto; else this.form.media.images.push(this.newPhoto);
                    this.newPhoto = ''; this.error = '';
                },
                makePrimary(url) { if (this.draft.published_listing_id) return; const rest = this.photos.filter(photo => photo !== url); this.form.media.primary_image = url; this.form.media.images = rest; this.photoIndex = 0; },
                removePhoto(url) {
                    if (this.preservedPhotos.includes(url)) return;
                    if (this.form.media.primary_image === url) this.form.media.primary_image = '';
                    this.form.media.images = this.form.media.images.filter(photo => photo !== url); this.photoIndex = 0;
                },
                cancelTypeSearch() { clearTimeout(typeTimer); typeController?.abort(); typeRevision++; this.typeLoading = false; },
                searchTypes() {
                    this.cancelTypeSearch(); this.selectedType = null; this.typeError = ''; this.typeOptions = []; const revision = typeRevision;
                    if (!this.typeQuery.trim()) { this.typeOptions = this.initialTypeOptions(); return; }
                    this.typeLoading = true;
                    typeTimer = setTimeout(async () => {
                        typeController = new AbortController();
                        try { const result = await this.request(config.urls.types + '?query=' + encodeURIComponent(this.typeQuery.trim()) + '&limit=30', {signal:typeController.signal}); if (alive && revision === typeRevision) this.typeOptions = result.items; }
                        catch (error) { if (alive && revision === typeRevision && error.name !== 'AbortError') this.typeError = this.readableError(error); }
                        finally { if (alive && revision === typeRevision) this.typeLoading = false; }
                    }, 250);
                },
                impactValue(value) {
                    if (value === null || value === undefined) return 'Не будет в черновике';
                    if (value.new_mapping_requested) return 'Будет сохранено новое соответствие по вашему выбору';
                    if (value.id && !value.values) return 'Сохранённое соответствие №' + value.id;
                    if (Array.isArray(value.values)) return value.values.map(item => String(item?.value ?? '')).join(' · ') || 'Пустое значение';
                    if (value.attribute_id) return 'Характеристика ' + value.attribute_id + (value.complex_id && value.complex_id !== '0' ? ' · группа ' + value.complex_id : '');
                    return String(value);
                },
                async applyType() {
                    if (!this.selectedType || this.locked || this.loading) return;
                    if (this.linkedCategory.mode !== 'new' && (
                        this.linkedCategory.mode !== 'repair' ||
                        this.selectedType.id !== this.linkedCategory.observed_type?.id || this.saveMapping
                    )) {
                        this.typeError = 'Для существующей карточки доступно только восстановление типа из свежего точного листинга Ozon.';
                        return;
                    }
                    if (this.dirty) { this.typeError = 'Сначала сохраните текущие правки. Выбранный тип останется на экране.'; this.$nextTick(() => document.querySelector('.ode-savebar button')?.focus()); return; }
                    dialogReturnFocus = document.activeElement;
                    this.typeError = '';
                    this.categoryReview = {open:true, loading:false, saving:false, rows:[], total:0, page:0, hasMore:false, digest:'', token:'', target:{...this.selectedType}, initialType:this.draft.product_type_id, mapping:!!this.saveMapping, viewedVersion:this.draft.version, confirmed:false, error:'', pending:null, needsChoice:false};
                    this.$nextTick(() => { this.$refs.categoryDialog.showModal(); this.$refs.categoryReviewTitle?.focus(); });
                    await this.loadCategoryImpact(1);
                },
                prepareLinkedCategoryRepair() {
                    if (this.linkedCategory.mode !== 'repair' || this.locked || this.loading) return;
                    this.selectedType = {...this.linkedCategory.observed_type};
                    this.saveMapping = false;
                    this.applyType();
                },
                closeCategoryReview() {
                    if (this.categoryReview.saving) return;
                    categoryController?.abort(); this.$refs.categoryDialog?.close(); this.categoryReview.open = false;
                    dialogReturnFocus?.focus?.();
                },
                async loadCategoryImpact(page = 1) {
                    const review = this.categoryReview;
                    if (!review.open || review.loading || review.saving || review.needsChoice) return;
                    categoryController?.abort(); categoryController = new AbortController();
                    const current = categoryController;
                    review.loading = true; review.error = '';
                    const params = new URLSearchParams({expected_version:String(review.viewedVersion), target_product_type_id:String(review.target.id), save_mapping:String(review.mapping), page:String(page)});
                    try {
                        const result = await this.request(config.urls.categoryImpact + '?' + params, {signal:current.signal, timeoutMs:10000});
                        if (!alive || categoryController !== current || !review.open) return;
                        if (result.version !== review.viewedVersion || result.draft_id !== this.draft.id || result.target_type?.id !== review.target.id || result.save_mapping !== review.mapping || (page > 1 && result.digest !== review.digest)) throw Error('Состояние проверки изменилось. Перечитайте сохранённый черновик.');
                        if (page === 1) review.rows = result.rows; else review.rows.push(...result.rows);
                        review.page = page; review.total = result.total; review.hasMore = result.has_more; review.digest = result.digest; review.token = result.review_token; review.confirmed = false;
                    } catch (error) {
                        if (alive && categoryController === current && error.name !== 'AbortError') {
                            review.error = this.readableError(error);
                            if (error.status === 409) { review.needsChoice = true; this.conflict = true; }
                        }
                    } finally { if (alive && categoryController === current) review.loading = false; }
                },
                async readCategoryCurrent() {
                    const review = this.categoryReview;
                    if (!review.open || review.loading || review.saving) return;
                    review.loading = true; review.error = '';
                    try {
                        const result = await this.request(config.urls.editor, {timeoutMs:10000});
                        if (!alive || !review.open) return;
                        review.pending = result; review.needsChoice = true; review.confirmed = false; review.token = '';
                    } catch (error) { if (alive) review.error = this.readableError(error); }
                    finally { if (alive) review.loading = false; }
                },
                adoptCategoryCurrent() {
                    const review = this.categoryReview;
                    if (!review.pending || review.loading || review.saving) return;
                    const selected = review.target, query = this.typeQuery, mapping = review.mapping;
                    this.hydrate(review.pending); this.selectedType = selected; this.typeQuery = query; this.saveMapping = mapping;
                    if (this.linkedCategory.mode !== 'new' && (
                        this.linkedCategory.mode !== 'repair' || selected.id !== this.linkedCategory.observed_type?.id
                    )) {
                        this.notice = 'Состояние листинга изменилось. Проверьте актуальную категорию.';
                        this.closeCategoryReview(); return;
                    }
                    if (!this.typeOptions.some(item => item.id === selected.id)) this.typeOptions = [selected, ...this.typeOptions];
                    this.conflict = false; this.uncertain = false; this.error = '';
                    if (review.initialType !== selected.id && this.draft.product_type_id === selected.id) {
                        this.notice = 'Категория уже сохранена в черновике. Повторная запись не нужна.';
                        this.closeCategoryReview(); return;
                    }
                    review.pending = null; review.needsChoice = false; review.viewedVersion = this.draft.version;
                    review.initialType = this.draft.product_type_id;
                    review.rows = []; review.total = 0; review.page = 0; review.token = ''; review.digest = ''; review.confirmed = false;
                    this.loadCategoryImpact(1);
                },
                async confirmCategory() {
                    const review = this.categoryReview;
                    if (!review.open || review.loading || review.saving || review.hasMore || !review.confirmed || !review.token || review.needsChoice || this.dirty || this.locked || this.draft.version !== review.viewedVersion) return;
                    review.saving = true; review.error = '';
                    const success = await this.mutate('save', {expected_version:review.viewedVersion, patch:{product_type_id:review.target.id, save_mapping:review.mapping}, category_review_token:review.token}, 'Категория выбрана. Проверьте сохранённые характеристики перед отправкой.');
                    review.saving = false; review.confirmed = false; review.token = '';
                    if (success) this.closeCategoryReview();
                    else if (this.conflict || this.uncertain) review.needsChoice = true;
                    else review.error = this.error || 'Не удалось применить категорию.';
                },
                openDictionary(def, groupIndex = null) {
                    if (this.locked || !this.schemaFresh || !def.dictionary_fresh) return;
                    dialogReturnFocus = document.activeElement;
                    this.dictionary = {open:true, definition:def, groupIndex, query:'', items:[], loading:false, error:'', more:false};
                    this.$nextTick(() => { this.$refs.dictionaryDialog.showModal(); this.$refs.dictionarySearch.focus(); }); this.searchDictionary();
                },
                closeDictionary() { clearTimeout(searchTimer); searchController?.abort(); searchRevision++; this.$refs?.dictionaryDialog?.close(); this.dictionary.open = false; dialogReturnFocus?.focus?.(); },
                searchDictionary() {
                    clearTimeout(searchTimer); searchController?.abort(); const revision = ++searchRevision;
                    const typeId = this.draft.product_type_id, defId = this.dictionary.definition.id, query = this.dictionary.query;
                    this.dictionary.loading = true; this.dictionary.items = []; this.dictionary.error = ''; this.dictionary.more = false;
                    searchTimer = setTimeout(async () => {
                        searchController = new AbortController();
                        try {
                            const result = await this.request(config.urls.dictionary + encodeURIComponent(defId) + '?product_type_id=' + typeId + '&q=' + encodeURIComponent(query), {signal:searchController.signal});
                            if (!alive || revision !== searchRevision || !this.dictionary.open || this.draft.product_type_id !== typeId) return;
                            if (result.product_type_id !== typeId || result.attribute_id !== defId) throw Error('Категория ответа не совпала. Повторите поиск.');
                            this.dictionary.items = result.items; this.dictionary.more = result.has_more;
                        } catch (error) { if (alive && revision === searchRevision && error.name !== 'AbortError') this.dictionary.error = this.readableError(error); }
                        finally { if (alive && revision === searchRevision) this.dictionary.loading = false; }
                    }, 250);
                },
                chooseValue(value) {
                    if (!this.dictionary.items.some(row => row.id === value.id && row.value === value.value)) return;
                    const def = this.dictionary.definition, group = this.dictionary.groupIndex;
                    const current = this.valuesFor(def, group); let next = [{dictionary_value_id:value.id, value:value.value}];
                    if (def.collection) {
                        if (current.some(row => row.dictionary_value_id === value.id)) { this.closeDictionary(); return; }
                        if (current.length >= def.max_values) { this.dictionary.error = 'Достигнут предел значений для этой характеристики.'; return; }
                        next = [...clone(current), ...next];
                    }
                    this.setValues(def, next, group); this.closeDictionary();
                },
                openPublication() { if (!this.canPublish) return; dialogReturnFocus = document.activeElement; this.confirmation = 'publish'; this.confirmedWrite = false; this.$nextTick(() => this.$refs.publicationDialog.showModal()); },
                closePublication() { if (this.busy) return; this.$refs.publicationDialog?.close(); this.confirmation = null; dialogReturnFocus?.focus?.(); },
                async publish() {
                    if (!this.canPublish || !this.confirmedWrite) return;
                    this.busy = 'publish'; this.error = '';
                    const body = {expected_version:this.draft.version, idempotency_key:config.idempotencyKey + ':v' + this.draft.version};
                    if (this.draft.published_listing_id) body.confirm_write = true;
                    try {
                        const result = await this.request(this.draft.published_listing_id ? config.urls.updatePublication : config.urls.publish, {method:'POST', body:JSON.stringify(body)});
                        this.uncertain = true;
                        if (alive) { this.busy = ''; global.location.assign(this.operationUrl(result.operation.id)); }
                    } catch (error) {
                        if (alive) { this.conflict = error.status === 409; this.uncertain = !error.status || error.status >= 500; this.error = this.readableError(error); this.$refs.publicationDialog.close(); this.confirmation = null; }
                    } finally { if (alive) this.busy = ''; }
                },
            },
            mounted() {
                beforeUnload = event => { if (this.dirty || this.busy) { event.preventDefault(); event.returnValue = ''; } };
                global.addEventListener('beforeunload', beforeUnload); this.load(); document.getElementById('ode-boot-fallback')?.remove();
            },
            beforeUnmount() { alive = false; loadController?.abort(); categoryController?.abort(); this.cancelTypeSearch(); this.closeDictionary(); global.removeEventListener('beforeunload', beforeUnload); },
        };
    }
    global.ozonDraftEditor = {createOptions, serialize, safeImage, fieldComponent, photoComponent};
    const bootstrap = global.document?.getElementById('ode-bootstrap');
    if (bootstrap && global.Vue) global.Vue.createApp(createOptions(JSON.parse(bootstrap.textContent))).mount('#ozon-draft-editor');
})(window);
