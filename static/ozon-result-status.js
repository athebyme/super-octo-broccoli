/* Live result notice. Reads never reload the document or discard a repair form. */
(function (global) {
    'use strict';
    function createOptions(config) {
        let timer, controller, stopped = false, visibility;
        const signature = value => JSON.stringify([value.status, value.version, value.summary, value.item_results, value.error_code, value.error_message]);
        const initial = signature(config.initial);
        return {
            data: () => ({current:config.initial, changed:false, error:'', busy:false, failures:0}),
            computed: {
                active() { return config.kind === 'run' ? this.current.status === 'running' : ['queued','submitting','submitted','polling','uncertain'].includes(this.current.status) && !!this.current.next_poll_at; },
                automaticStopped() { return config.kind === 'operation' && this.current.status === 'uncertain' && !this.current.next_poll_at; },
                cooldownLabel() {
                    if (config.kind !== 'operation' || !['queued','submitting','submitted','polling','uncertain'].includes(this.current.status)) return '';
                    const raw = this.current.request_summary?.provider_read_not_before;
                    if (typeof raw !== 'string') return '';
                    const due = new Date(/[Zz]|[+-]\d\d:\d\d$/.test(raw) ? raw : raw + 'Z');
                    if (!Number.isFinite(due.getTime()) || due.getTime() <= Date.now()) return '';
                    if (due.getUTCFullYear() > 9998) return 'Ozon сообщил слишком долгую паузу. Нужна проверка доступа к API.';
                    return 'Ozon попросил паузу. Новая проверка доступна не раньше ' + due.toLocaleString('ru-RU') + ' (ваше время).';
                },
                label() {
                    if (config.kind === 'run') return {running:'Выполняется',success:'Завершена',partial:'Завершена частично',attention:'Нужно ваше внимание'}[this.current.summary?.outcome] || 'Результат обновлён';
                    return {queued:'В очереди',submitting:'Отправляется',submitted:'Ozon принял задачу',polling:'Проверяем результат',uncertain:'Нужна сверка',succeeded:'Выполнено',partial:'Выполнено частично',failed:'Не выполнено',cancelled:'Остановлено'}[this.current.status] || 'Результат обновлён';
                },
            },
            methods: {
                schedule() {
                    clearTimeout(timer);
                    if (!stopped && this.active && !document.hidden) timer = setTimeout(() => this.refresh(), Math.min(30000, 5000 * (1 + this.failures)));
                },
                async refresh() {
                    if (stopped || this.busy || document.hidden) return;
                    this.busy = true; controller = new AbortController();
                    const timeout = setTimeout(() => controller.abort(), 10000);
                    try {
                        const response = await fetch(config.url, {signal:controller.signal, credentials:'same-origin', headers:{Accept:'application/json'}});
                        if (response.status === 401 || response.redirected) { stopped = true; throw Error('Войдите снова, чтобы проверить результат. Введённые данные остаются на странице.'); }
                        if (!response.ok || !(response.headers.get('content-type') || '').includes('application/json')) throw Error('Не удалось обновить статус. Сохранённый результат доступен ниже.');
                        const body = await response.json(), value = body[config.kind];
                        if (body.success === false || !value || typeof value.status !== 'string') throw Error('Не удалось подтвердить новый статус. Повторите проверку позже.');
                        if (stopped) return;
                        this.current = value; this.changed = signature(value) !== initial; this.error = ''; this.failures = 0;
                    } catch (error) {
                        if (!document.hidden) this.error = error instanceof TypeError || error.name === 'AbortError' ? 'Соединение прервано. Сохранённый результат доступен ниже.' : error.message;
                        this.failures += 1;
                    } finally { clearTimeout(timeout); this.busy = false; this.schedule(); }
                },
            },
            mounted() { visibility = () => { clearTimeout(timer); if (document.hidden) controller?.abort(); else this.schedule(); }; document.addEventListener('visibilitychange', visibility); this.schedule(); },
            beforeUnmount() { stopped = true; clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visibility); },
        };
    }
    global.ozonResultStatus = {createOptions};
    const bootstrap = document.getElementById('ozon-result-bootstrap');
    if (bootstrap && global.Vue) global.Vue.createApp(createOptions(JSON.parse(bootstrap.textContent))).mount('#ozon-result-status');
})(window);
