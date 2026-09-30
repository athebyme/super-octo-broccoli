/* Seller-owned AI suggestions progress; generation never publishes to Ozon. */
(function (global) {
    'use strict';
    const boot = document.getElementById('oai-run-bootstrap');
    if (!boot || !global.Vue) return;
    const config = JSON.parse(boot.textContent);
    const uid = config.run.job_uid, accountId = config.run.account_id;
    const validRun = row => row && row.job_uid === uid && row.account_id === accountId &&
        row.mode === 'draft_suggestions' && Array.isArray(row.items) && row.items.length <= 200;
    if (!/^ozon-ai-[0-9a-f]{32}$/.test(uid) || !validRun(config.run)) return;
    const app = global.Vue.createApp({
        data() { return {config, run:config.run, csrf:config.csrf_token, busy:'', error:'',
            sessionEnded:false, cancelUncertain:false, confirmCancel:false}; },
        computed: {
            active() { return ['pending','running','cancelling'].includes(this.run.status); },
            done() { return this.run.items.filter(item => !['pending','reserved'].includes(item.status)).length; },
            rejectedCount() {
                const reasons = this.run.summary?.rejection_reasons;
                if (!reasons || typeof reasons !== 'object' || Array.isArray(reasons)) return 0;
                const count = Object.values(reasons).reduce((total, value) =>
                    total + (Number.isSafeInteger(value) && value > 0 ? value : 0), 0);
                return Math.min(this.run.total, count);
            },
            outcomeSummary() {
                const countText = count => count === 1 ? '1 карточки' : `${count} карточек`;
                const noEvidence = this.run.items.filter(item =>
                    item.status === 'no_evidence' && item.code !== 'ai_fields_rejected').length;
                const unknown = this.run.items.filter(item => item.status === 'unknown_response').length;
                const messages = [];
                if (this.rejectedCount) messages.push(`${this.rejectionSummary()} Откройте карточки, проверьте источник и заполните поля вручную или запустите новый поиск явно.`);
                if (noEvidence) messages.push(`Модель не вернула подтверждённых предложений для ${countText(noEvidence)}. Проверьте исходные сведения, затем заполните поля вручную или запустите новый поиск явно.`);
                if (unknown) messages.push(`Ответ модели не подтверждён для ${countText(unknown)}. Проверьте карточки и историю запуска; автоматического повтора нет. Новый поиск можно начать только вручную.`);
                return messages.join(' ');
            },
        },
        methods: {
            statusLabel(status) { return ({pending:'В очереди',running:'Обрабатывается',cancelling:'Отменяем',
                completed:'Завершён',cancelled:'Отменён',failed:'Не выполнен'})[status] || 'Требует проверки'; },
            itemLabel(status) { return ({pending:'В очереди',reserved:'Обрабатывается',proposed:'Предложения готовы',
                no_evidence:'Нет подтверждённых предложений',needs_input:'Нужно заполнить вручную',
                stale:'Исходные данные изменились',unknown_response:'Ответ модели неизвестен',
                failed:'Не удалось обработать',cancelled:'Отменено'})[status] || 'Требует проверки'; },
            itemCode(code) { return ({source_snapshot_missing:'Нет исходного снимка товара',
                schema_stale:'Требования Ozon требуют обновления',
                no_eligible_missing_attributes:'Для этой версии нет подходящих пустых характеристик',
                ai_capacity_full:'Дождитесь завершения текущих AI-запусков'})[code] ||
                (code && !['ai_fields_rejected','unknown_response'].includes(code) ? 'Проверьте карточку вручную' : ''); },
            itemGuidance(item) {
                if (item.status === 'unknown_response')
                    return 'Ответ модели не подтверждён. Проверьте состояние карточки и историю запуска; автоматического повтора нет. При необходимости откройте карточку и запустите новый поиск вручную либо заполните поля сами.';
                if (item.code === 'ai_fields_rejected') {
                    if (item.status === 'no_evidence')
                        return 'Модель вернула предложения, но проверка источника их отклонила; ничего не сохранено. Проверьте источник, затем заполните поля вручную или запустите новый поиск явно.';
                    return 'Часть предложений не прошла проверку источника и не сохранена. Проверьте доступные предложения; остальные поля заполните вручную или запустите новый поиск после проверки источника.';
                }
                if (item.status === 'no_evidence')
                    return 'Модель не вернула подтверждённых предложений. Проверьте исходные сведения, затем заполните поля вручную или запустите новый поиск явно.';
                return '';
            },
            rejectionSummary() {
                const count = this.rejectedCount;
                if (!count) return '';
                return count === 1
                    ? 'Проверка источника отклонила AI-предложения в 1 карточке.'
                    : `Проверка источника отклонила AI-предложения в ${count} карточках.`;
            },
            editorUrl(item) { return config.urls.editorBase + item.draft_id + '?ai_item_id=' + item.id; },
            async request(url, post=false) {
                const abort = new AbortController(), timer = setTimeout(() => abort.abort(), post ? 25000 : 10000);
                try {
                    const response = await fetch(url, {method:post ? 'POST':'GET', credentials:'same-origin',
                        redirect:'manual', cache:'no-store', signal:abort.signal,
                        headers:{Accept:'application/json', ...(post ? {'Content-Type':'application/json','X-CSRFToken':this.csrf} : {})},
                        ...(post ? {body:'{}'} : {})});
                    if (response.status === 401 || response.type === 'opaqueredirect' || response.redirected)
                        throw Object.assign(Error('Сессия завершилась. Войдите снова и перечитайте состояние.'), {status:401});
                    if (response.status === 403)
                        throw Object.assign(Error('Доступ к этому магазину недоступен.'), {status:403});
                    if (!(response.headers.get('content-type') || '').includes('application/json'))
                        throw Error('Состояние запуска не подтверждено. Повторите только чтение.');
                    const data = await response.json();
                    if (typeof data.csrf_token === 'string' && data.csrf_token) this.csrf = data.csrf_token;
                    if (!response.ok || data.success !== true)
                        throw Object.assign(Error(data.error || 'Не удалось получить состояние.'), {status:response.status});
                    if (!validRun(data.run)) throw Error('Ответ относится к другому запуску или магазину.');
                    return data.run;
                } finally { clearTimeout(timer); }
            },
            async refresh() {
                if (this.busy || this.sessionEnded || document.hidden) return;
                this.busy = 'read'; this.error = '';
                try { this.run = await this.request(config.urls.status); this.cancelUncertain = false; }
                catch (error) {
                    if (error.status === 401 || error.status === 403) this.sessionEnded = true;
                    this.error = error.name === 'AbortError' ? 'Проверка заняла слишком много времени. Повторите чтение.' :
                        error instanceof TypeError ? 'Нет связи. Повторите чтение.' : error.message;
                } finally { this.busy = ''; this.schedule(); }
            },
            schedule() {
                clearTimeout(this._timer);
                if (this.active && !this.sessionEnded && !document.hidden)
                    this._timer = setTimeout(() => this.refresh(), 5000);
            },
            openCancel() {
                if (!this.active || this.busy || this.sessionEnded) return;
                this.confirmCancel = false; this.$refs.cancelDialog.showModal();
                this.$nextTick(() => this.$refs.cancelCheck?.focus());
            },
            closeCancel() {
                if (this.busy === 'cancel') return;
                this.$refs.cancelDialog?.close(); this.confirmCancel = false;
                this.$refs.cancelButton?.focus();
            },
            async cancel() {
                if (!this.active || !this.confirmCancel || this.busy || this.sessionEnded) return;
                this.busy = 'cancel'; this.error = '';
                try { this.run = await this.request(config.urls.cancel, true); this.cancelUncertain = false; }
                catch (error) {
                    if (error.status === 401 || error.status === 403) this.sessionEnded = true;
                    this.cancelUncertain = !error.status || error.status >= 500;
                    this.error = error.name === 'AbortError' ? 'Ответ отмены неизвестен. Прочитайте состояние запуска перед новым действием.' :
                        error instanceof TypeError ? 'Нет связи. Прочитайте состояние запуска.' : error.message;
                } finally { this.busy = ''; this.$refs.cancelDialog?.close(); this.confirmCancel = false; this.schedule(); }
            },
            visibility() { if (document.hidden) clearTimeout(this._timer); else this.refresh(); },
        },
        mounted() { document.addEventListener('visibilitychange', this.visibility); this.schedule();
            document.getElementById('oai-run-fallback')?.remove(); },
        beforeUnmount() { clearTimeout(this._timer); document.removeEventListener('visibilitychange', this.visibility); },
    });
    app.mount('#ozon-ai-run');
})(window);
