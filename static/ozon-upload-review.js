/* Exact, seller-scoped review before a separate Ozon publication run. */
(function (global) {
    'use strict';
    const boot = document.getElementById('our-bootstrap');
    if (!boot || !global.Vue) return;
    const config = JSON.parse(boot.textContent), initial = config.review;
    const scope = 'ozon-upload-review-v1:' + initial.account_id + ':' +
        initial.draft_ids.join(',') + ':' + (initial.parent_prepare_job_uid || '');
    const selectionKey = scope + ':selection', pendingKey = scope + ':pending';
    const runPattern = /^ozon-upload-[0-9a-f]{32}$/;
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const positive = value => Number.isSafeInteger(value) && value > 0;
    const read = key => { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_) { return null; } };
    const hasStored = key => { try { return sessionStorage.getItem(key) !== null; } catch (_) { return false; } };
    const store = (key, value) => { try { sessionStorage.setItem(key, JSON.stringify(value)); return true; } catch (_) { return false; } };
    const forget = key => { try { sessionStorage.removeItem(key); } catch (_) {} };
    function selectedRows(value) {
        if (!Array.isArray(value) || value.length > 200) return [];
        const allowed = new Set(initial.draft_ids), seen = new Set();
        return value.filter(row => row && positive(row.id) && positive(row.version) &&
            allowed.has(row.id) && !seen.has(row.id) && (seen.add(row.id), true));
    }
    function validPending(value) {
        if (!value || value.account_id !== initial.account_id ||
            typeof value.request_key !== 'string' || !/^[A-Za-z0-9_-]{24,128}$/.test(value.request_key) ||
            value.parent_prepare_job_uid !== initial.parent_prepare_job_uid ||
            !Array.isArray(value.draft_ids) || !value.draft_ids.length || value.draft_ids.length > 200 ||
            !value.draft_ids.every(id => positive(id) && initial.draft_ids.includes(id)) ||
            new Set(value.draft_ids).size !== value.draft_ids.length ||
            !value.expected_versions || typeof value.expected_versions !== 'object' ||
            Array.isArray(value.expected_versions) ||
            !same(Object.keys(value.expected_versions).sort(), value.draft_ids.map(String).sort()) ||
            !Object.values(value.expected_versions).every(positive)) return false;
        return true;
    }
    function params(ids, page) {
        const query = new URLSearchParams({account_id:String(initial.account_id),
            draft_ids:ids.join(','), page:String(page)});
        if (initial.parent_prepare_job_uid) query.set('parent_prepare_job_uid', initial.parent_prepare_job_uid);
        return query;
    }
    async function getJson(url, headers={}) {
        const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 10000);
        try {
            const response = await fetch(url, {credentials:'same-origin', cache:'no-store',
                redirect:'manual', headers:{Accept:'application/json', ...headers}, signal:abort.signal});
            if (response.status === 401 || response.type === 'opaqueredirect' || response.redirected)
                throw Object.assign(Error('Сессия завершилась. Войдите снова и проверьте состояние.'), {status:401});
            if (response.status === 403)
                throw Object.assign(Error('Доступ к этому магазину больше недоступен.'), {status:403});
            if (!(response.headers.get('content-type') || '').includes('application/json'))
                throw Error('Не удалось прочитать ответ сервера.');
            const data = await response.json();
            if (!response.ok || data.success !== true)
                throw Object.assign(Error(data.error || 'Не удалось проверить карточки.'),
                    {status:response.status, csrf_token:data.csrf_token});
            return data;
        } finally { clearTimeout(timer); }
    }
    const app = global.Vue.createApp({
        data() { const storedPending = read(pendingKey); return {config, review:initial, selected:selectedRows(read(selectionKey)),
            pending:validPending(storedPending) ? storedPending : null,
            storageProblem:hasStored(pendingKey) && !validPending(storedPending),
            csrf:initial.csrf_token, busy:'', error:'', status:'',
            reviewed:null, confirmed:false, sessionEnded:false, retrySameKey:false,
            staleIds:[], alive:true}; },
        computed: {
            selectedIds() { return this.selected.map(row => row.id); },
            locked() { return !!this.busy || !!this.pending || this.storageProblem || this.sessionEnded; },
            canReview() { return this.selected.length > 0 && !this.locked; },
        },
        methods: {
            pageUrl(page) { return config.urls.html + '?' + params(initial.draft_ids, page); },
            editorUrl(row) { return config.urls.editorBase + row.draft_id; },
            imageUrl(row) {
                const value = row.primary_image;
                if (typeof value !== 'string' || value.length > 2000) return '';
                const local = value.match(/^\/api\/photos\/imported-product\/([1-9]\d*)\/(0|[1-9]\d*)$/);
                if (local) return String(row.imported_product_id) === local[1] ? value + '?deferred=1' : '';
                return global.ozonDraftEditor?.safeImage(value) || '';
            },
            sellerPrice(row) {
                const price = row.commercial?.price;
                return row.commercial?.currency_code === 'RUB' && Number(price) > 0
                    ? String(price) + ' ₽' : 'не указана или валюта не подтверждена';
            },
            oldPrice(row) {
                const price = row.commercial?.old_price;
                return row.commercial?.currency_code === 'RUB' && Number(price) > 0
                    ? String(price) + ' ₽' : '';
            },
            packageLabel(row) {
                const d = row.dimensions || {}, unit = {MILLIMETERS:'мм', CENTIMETERS:'см', INCHES:'дюйм'}[d.dimension_unit];
                const weightUnit = {GRAMS:'г', KILOGRAMS:'кг', POUNDS:'фунт'}[d.weight_unit];
                const size = unit && [d.width,d.height,d.depth].every(value => Number(value) > 0)
                    ? [d.width,d.height,d.depth].join(' × ') + ' ' + unit : 'габариты не заполнены';
                const weight = weightUnit && Number(d.weight) > 0 ? String(d.weight) + ' ' + weightUnit : 'вес не заполнен';
                return size + ' · ' + weight;
            },
            picked(row) { return this.selected.some(item => item.id === row.draft_id && item.version === row.version); },
            reconcilePage() {
                const changed = [];
                this.selected = this.selected.filter(item => {
                    const current = this.review.items.find(row => row.draft_id === item.id);
                    if (!current) return true;
                    if (current.selectable && current.version === item.version) return true;
                    changed.push(item.id); return false;
                });
                if (changed.length) {
                    this.staleIds = changed;
                    this.status = 'Часть выбранных карточек изменилась. Просмотрите их снова.';
                    store(selectionKey, this.selected);
                }
            },
            toggle(row, checked) {
                if (this.locked || !row.selectable) return;
                this.reviewed = null; this.confirmed = false;
                this.selected = this.selected.filter(item => item.id !== row.draft_id);
                if (checked) this.selected.push({id:row.draft_id, version:row.version});
                this.selected.sort((a,b) => initial.draft_ids.indexOf(a.id) - initial.draft_ids.indexOf(b.id));
                this.error = store(selectionKey, this.selected) ? '' :
                    'Браузер не сохранил выбор между страницами. Проверьте настройки хранения данных сайта.';
            },
            clear() {
                if (this.locked) return;
                this.selected = []; this.reviewed = null; this.confirmed = false;
                forget(selectionKey); this.status = 'Выбор снят.';
            },
            clearStorageProblem() {
                if (!this.storageProblem || this.busy) return;
                forget(pendingKey); this.storageProblem = false;
                this.status = 'Повреждённая запись снята после вашей проверки истории. Перед отправкой ещё раз просмотрите карточки.';
            },
            async reviewSelected() {
                if (!this.canReview) return;
                this.busy = 'read'; this.error = ''; this.status = ''; this.reviewed = null;
                const ids = [...this.selectedIds], rows = [];
                try {
                    for (let page=1, count=Math.ceil(ids.length/20); page<=count; page++) {
                        const data = await getJson(config.urls.api + '?' + params(ids, page));
                        const doc = data.review;
                        if (!doc || doc.account_id !== initial.account_id || !same(doc.draft_ids, ids) ||
                            doc.parent_prepare_job_uid !== initial.parent_prepare_job_uid ||
                            doc.pagination?.page !== page || doc.pagination?.total !== ids.length ||
                            doc.pagination?.per_page !== 20 || !Array.isArray(doc.items))
                            throw Error('Магазин или состав карточек изменился. Начните просмотр заново.');
                        if (typeof doc.csrf_token !== 'string' || !doc.csrf_token)
                            throw Error('Защита формы не подтверждена. Обновите просмотр.');
                        this.csrf = doc.csrf_token;
                        rows.push(...doc.items);
                    }
                    if (rows.length !== ids.length || !same(rows.map(row => row.draft_id), ids))
                        throw Error('Список карточек изменился. Начните просмотр заново.');
                    const stale = rows.filter(row => !row.selectable ||
                        this.selected.find(item => item.id === row.draft_id)?.version !== row.version);
                    if (stale.length) {
                        const changed = new Set(stale.map(row => row.draft_id));
                        this.selected = this.selected.filter(item => !changed.has(item.id));
                        store(selectionKey, this.selected); this.staleIds = [...changed];
                        this.error = 'Часть карточек изменилась или теперь требует исправления. Их выбор снят; откройте эти карточки и проверьте снова.';
                        return;
                    }
                    this.reviewed = rows; this.confirmed = false;
                    this.$nextTick(() => { this.$refs.dialog?.showModal(); this.$refs.confirm?.focus(); });
                } catch (error) {
                    if (!this.alive) return;
                    if (typeof error.csrf_token === 'string' && error.csrf_token) this.csrf = error.csrf_token;
                    if (error.status === 401 || error.status === 403) this.sessionEnded = true;
                    this.error = error.name === 'AbortError' ? 'Проверка заняла слишком много времени. Выбор сохранён; повторите чтение.' :
                        error instanceof TypeError ? 'Нет ответа сервера. Выбор сохранён; проверьте соединение.' : error.message;
                } finally { if (this.alive) this.busy = ''; }
            },
            closeDialog() {
                if (this.busy === 'post') return;
                this.$refs.dialog?.close(); this.confirmed = false; this.$refs.reviewButton?.focus();
            },
            async publish() {
                if (!this.reviewed?.length || !this.confirmed || this.locked) return;
                const ids = this.reviewed.map(row => row.draft_id);
                const versions = Object.fromEntries(this.reviewed.map(row => [String(row.draft_id), row.version]));
                const key = global.crypto?.randomUUID?.().replace(/-/g, '') ||
                    Array.from(global.crypto.getRandomValues(new Uint8Array(16)),
                        byte => byte.toString(16).padStart(2,'0')).join('');
                const pending = {request_key:key, account_id:initial.account_id, draft_ids:ids,
                    expected_versions:versions, parent_prepare_job_uid:initial.parent_prepare_job_uid};
                if (!store(pendingKey, pending)) {
                    this.error = 'Браузер не сохранил ключ действия. Отправка остановлена, чтобы не потерять её результат.';
                    this.closeDialog(); return;
                }
                this.pending = pending;
                await this.submitPending(pending);
            },
            async retryPending() {
                if (!this.pending || !this.retrySameKey || this.busy || this.sessionEnded || this.storageProblem) return;
                this.retrySameKey = false;
                await this.submitPending(this.pending);
            },
            async submitPending(pending) {
                if (!validPending(pending) || this.busy || this.sessionEnded) return;
                this.busy = 'post'; this.error = '';
                const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 45000);
                try {
                    const response = await fetch(config.urls.publish, {method:'POST',
                        credentials:'same-origin', redirect:'manual', signal:abort.signal,
                        headers:{Accept:'application/json','Content-Type':'application/json','X-CSRFToken':this.csrf},
                        body:JSON.stringify({account_id:pending.account_id,draft_ids:pending.draft_ids,
                            expected_versions:pending.expected_versions,confirm_write:true,request_key:pending.request_key,
                            ...(pending.parent_prepare_job_uid ? {parent_prepare_job_uid:pending.parent_prepare_job_uid} : {})})});
                    if (response.status === 401 || response.type === 'opaqueredirect' || response.redirected)
                        throw Object.assign(Error('Сессия завершилась. Войдите снова и проверьте результат по сохранённому ключу.'), {status:401});
                    if (response.status === 403)
                        throw Object.assign(Error('Доступ к магазину больше недоступен.'), {status:403});
                    if (!(response.headers.get('content-type') || '').includes('application/json'))
                        throw Error('Ответ отправки не подтверждён. Проверьте результат по сохранённому ключу.');
                    const data = await response.json();
                    if (!response.ok || data.success !== true) {
                        if (response.status < 500) { forget(pendingKey); this.pending = null; this.reviewed = null; }
                        throw Object.assign(Error(data.error || 'Отправка не принята.'), {status:response.status});
                    }
                    if (data.run?.mode !== 'reviewed_drafts' || data.run.account_id !== pending.account_id ||
                        !runPattern.test(data.run.job_uid))
                        throw Error('Ответ не совпал с просмотренным действием. Проверьте результат по ключу.');
                    forget(pendingKey); this.pending = null;
                    global.location.assign(config.urls.runBase + data.run.job_uid);
                } catch (error) {
                    if (!this.alive) return;
                    if (error.status === 401 || error.status === 403) this.sessionEnded = true;
                    this.error = error.name === 'AbortError' ? 'Ответ отправки не пришёл вовремя. Повторная отправка заблокирована; проверьте результат.' :
                        error instanceof TypeError ? 'Связь прервалась. Повторная отправка заблокирована; проверьте результат.' : error.message;
                } finally {
                    clearTimeout(timer);
                    if (this.alive) { this.busy = ''; this.$refs.dialog?.close(); this.confirmed = false; }
                }
            },
            async readPending() {
                if (!this.pending || this.busy || this.sessionEnded) return;
                this.busy = 'readback'; this.error = ''; this.status = ''; this.retrySameKey = false;
                try {
                    const data = await getJson(config.urls.byRequest + '?account_id=' + this.pending.account_id,
                        {'X-Upload-Request-Key':this.pending.request_key});
                    if (data.run?.account_id !== this.pending.account_id ||
                        data.run?.mode !== 'reviewed_drafts' || !runPattern.test(data.run?.job_uid))
                        throw Error('Результат не совпал с просмотренным магазином и действием.');
                    if (typeof data.csrf_token === 'string' && data.csrf_token) this.csrf = data.csrf_token;
                    forget(pendingKey); this.pending = null;
                    global.location.assign(config.urls.runBase + data.run.job_uid);
                } catch (error) {
                    if (!this.alive) return;
                    if (typeof error.csrf_token === 'string' && error.csrf_token) this.csrf = error.csrf_token;
                    if (error.status === 401 || error.status === 403) this.sessionEnded = true;
                    if (error.status === 404) this.retrySameKey = true;
                    this.error = error.status === 404 ? 'Запуск по сохранённому ключу не найден. Это не доказывает, что первый запрос не был принят; можно повторить только то же действие с тем же ключом.' :
                        error.name === 'AbortError' ? 'Проверка результата заняла слишком много времени. Повторите только чтение.' :
                        error instanceof TypeError ? 'Нет связи. Повторите только чтение.' : error.message;
                } finally { if (this.alive) this.busy = ''; }
            },
        },
        watch: {
            error(value) { if (value) this.$nextTick(() => this.$refs.error?.focus()); },
        },
        mounted() { this.reconcilePage(); document.getElementById('our-boot-fallback')?.remove(); },
        beforeUnmount() { this.alive = false; },
    });
    app.component('draft-photo', global.ozonDraftEditor.photoComponent);
    app.mount('#ozon-upload-review');
})(window);
