/* Read-only progress plus explicit local POSTs. No secret is kept in reactive state. */
(function () {
    'use strict';
    function active(job) { return !!job && ['pending', 'running'].includes(job.status); }
    function date(value) {
        if (!value) return null;
        return new Date(/[Zz]|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z');
    }
    async function read(response) {
        if (response.status === 401 || response.redirected) {
            const error = new Error('Сессия истекла. Обновите страницу и войдите снова.');
            error.code = 'auth_required';
            throw error;
        }
        if (!(response.headers.get('content-type') || '').includes('application/json')) {
            throw new Error('Не удалось получить ответ. Проверьте соединение и повторите попытку.');
        }
        const data = await response.json();
        if (!response.ok || data.success === false) {
            const error = new Error(data.error || 'Не удалось выполнить действие. Повторите попытку.');
            error.code = data.code;
            throw error;
        }
        return data;
    }

    window.ozonAccountSetup = function () {
        let timer, pollController, visibilityHandler, landingHandler, stopped = false, revision = 0;
        const formControllers = new Set();
        const historyControllers = new Map();
        return {
            accounts: {}, jobs: {}, syncs: {}, busy: {new: false}, errors: {}, pollError: '',
            pollFailures: 0, config: {}, enabled: true, sessionExpired: false, uncertain: {}, conflicts: {}, reviewed: {}, catalogUpdated: false, keyOpen: {},
            actionModes: {}, settingsReview: {}, settingsReviewed: {}, saved: {}, history: {},
            settingsErrors: {}, settingsConflicts: {}, settingsUncertain: {},
            init(config) {
                this.config = config || JSON.parse(this.$el.dataset.ozonSetup || '{}');
                this.apply(this.config);
                const requested = new URL(location.href);
                const accountId = Number(requested.searchParams.get('account_id'));
                if (requested.searchParams.get('action') === 'replace-key' && Number.isSafeInteger(accountId) && this.accounts[accountId]) {
                    // Native fragment navigation happens after mount. Focus after
                    // load/paint, and never steal it from an already active form.
                    landingHandler = () => requestAnimationFrame(() => {
                        if (stopped || ['INPUT','TEXTAREA','SELECT','BUTTON'].includes(document.activeElement?.tagName)) return;
                        this.openKey(accountId, false);
                    });
                    if (document.readyState === 'complete') landingHandler();
                    else window.addEventListener('load', landingHandler, {once:true});
                }
                visibilityHandler = () => {
                    clearTimeout(timer);
                    if (document.hidden) {
                        if (pollController) pollController.abort();
                    } else if (this.enabled && !this.sessionExpired && Object.values(this.jobs).some(active)) {
                        this.refresh();
                    }
                };
                document.addEventListener('visibilitychange', visibilityHandler);
                this.schedule();
            },
            destroy() {
                stopped = true;
                clearTimeout(timer);
                document.removeEventListener('visibilitychange', visibilityHandler);
                if (landingHandler) window.removeEventListener('load', landingHandler);
                if (pollController) pollController.abort();
                formControllers.forEach(controller => controller.abort());
                historyControllers.forEach(controller => controller.abort());
            },
            apply(data) {
                Object.entries(data.onboarding_jobs || {}).forEach(([id, job]) => {
                    if (active(this.jobs[id]) && job.status === 'completed') this.catalogUpdated = true;
                });
                if (typeof data.ozon_enabled === 'boolean') this.enabled = data.ozon_enabled;
                (data.accounts || []).forEach(account => {
                    this.accounts[account.id] = account;
                    if (typeof this.busy[account.id] !== 'boolean') this.busy[account.id] = false;
                });
                if (data.onboarding_jobs) this.jobs = data.onboarding_jobs;
                if (data.catalog_syncs) this.syncs = data.catalog_syncs;
            },
            schedule() {
                clearTimeout(timer);
                if (stopped || document.hidden || !this.enabled || this.sessionExpired || !Object.values(this.jobs).some(active)) return;
                timer = setTimeout(() => this.refresh(), Math.min(30000, 5000 * (1 + this.pollFailures)));
            },
            async refresh() {
                if (stopped || document.hidden || pollController) return;
                pollController = new AbortController();
                const requestRevision = revision;
                const timeout = setTimeout(() => pollController && pollController.abort(), 15000);
                try {
                    const response = await fetch(this.config.status_url, {
                        credentials: 'same-origin', headers: {Accept: 'application/json'},
                        signal: pollController.signal, cache: 'no-store'
                    });
                    const data = await read(response);
                    if (stopped || requestRevision !== revision) return;
                    this.apply(data);
                    this.pollError = '';
                    this.sessionExpired = false;
                    this.pollFailures = 0;
                } catch (error) {
                    if (!stopped && !document.hidden) {
                        this.sessionExpired = error.code === 'auth_required';
                        this.pollError = this.sessionExpired ? error.message : 'Не удалось обновить статус. Фоновая загрузка не отменена.';
                        this.pollFailures += 1;
                    }
                } finally {
                    clearTimeout(timeout);
                    pollController = null;
                    this.schedule();
                }
            },
            async submit(event) {
                const form = event.currentTarget;
                if (!form.reportValidity()) return;
                const id = form.dataset.accountId || 'new';
                if (this.busy[id]) return;
                const payload = new FormData(form);
                const mode = form.dataset.accountAction || 'key';
                const errors = mode === 'settings' ? this.settingsErrors : this.errors;
                const conflicts = mode === 'settings' ? this.settingsConflicts : this.conflicts;
                const uncertain = mode === 'settings' ? this.settingsUncertain : this.uncertain;
                if (mode === 'settings' && (conflicts[id] || uncertain[id])) return;
                this.actionModes[id] = mode;
                this.busy[id] = true;
                revision += 1;
                errors[id] = '';
                conflicts[id] = false;
                uncertain[id] = false;
                if (mode === 'settings') {
                    this.saved[id] = '';
                    this.settingsReviewed[id] = false;
                    this.settingsReview[id] = null;
                } else this.reviewed[id] = false;
                const controller = new AbortController();
                formControllers.add(controller);
                const timeout = setTimeout(() => controller.abort(), 20000);
                try {
                    if (new URL(form.action, location.href).origin !== location.origin) {
                        throw new Error('Форма подключения должна отправляться только в Seller Hub.');
                    }
                    const response = await fetch(form.action, {
                        method: 'POST', credentials: 'same-origin',
                        headers: {Accept: 'application/json', 'X-CSRFToken':
                            document.querySelector('meta[name="csrf-token"]')?.content || form.elements.namedItem('csrf_token')?.value || ''},
                        body: payload,
                        signal: controller.signal
                    });
                    const data = await read(response);
                    if (stopped) return;
                    if (id !== 'new' && (!data.account || data.account.id !== Number(id)
                            || data.account.marketplace_code !== 'ozon'
                            || !Number.isSafeInteger(data.account.version) || data.account.version <= 0
                            || data.account.external_account_id !== this.accounts[id]?.external_account_id)) {
                        const error = new Error('Ответ относится к другому магазину. Перечитайте состояние перед повтором.');
                        error.code = 'unconfirmed_response';
                        throw error;
                    }
                    revision += 1;
                    // A saved key must not survive in the input, browser state
                    // or a second submission. Server never sends it back.
                    const key = form.elements.namedItem('api_key');
                    if (key) key.value = '';
                    if (data.account) this.accounts[data.account.id] = data.account;
                    if (data.job) this.jobs[data.job.account_id] = data.job;
                    if (data.setup_error) this.errors[id] = data.message;
                    if (mode === 'settings') {
                        // Only this explicit save owns this form's viewed version.
                        // Key/default/disconnect confirmations stay independent.
                        if (data.account.label !== String(payload.get('label')).trim()
                                || this.vatLabel(data.account.settings?.default_vat || '') !== this.vatLabel(payload.get('default_vat') || '')) {
                            const error = new Error('Настройки уже изменились после сохранения. Перечитайте текущее состояние.');
                            error.code = 'marketplace_account_version_conflict';
                            throw error;
                        }
                        form.elements.namedItem('expected_version').value = String(data.account.version);
                        this.saved[id] = data.message || 'Настройки сохранены. Существующие карточки не изменены.';
                        const details = document.getElementById('ozon-account-' + id)?.querySelector('[data-account-history]');
                        if (details?.open || this.history[id]) this.loadHistory(Number(id), false, true);
                    }
                    if (id === 'new' || form.dataset.reload === 'true') {
                        const target = new URL(this.config.accounts_url, location.origin);
                        if (data.account && Number.isSafeInteger(data.account.id)) {
                            target.searchParams.set('account_id', String(data.account.id));
                            target.hash = 'ozon-account-' + data.account.id;
                        }
                        if (data.setup_error) target.searchParams.set('setup_pending', '1');
                        location.assign(target.href);
                    } else {
                        this.schedule();
                    }
                } catch (error) {
                    if (!stopped) {
                        this.sessionExpired = error.code === 'auth_required';
                        conflicts[id] = error.code === 'marketplace_account_version_conflict';
                        uncertain[id] = error.name === 'AbortError' || error instanceof TypeError || error.code === 'unconfirmed_response';
                        errors[id] = error.name === 'AbortError' || error instanceof TypeError
                            ? 'Ответ не получен. Действие могло сохраниться — обновите статус перед повтором.'
                            : error.message;
                    }
                } finally {
                    clearTimeout(timeout);
                    formControllers.delete(controller);
                    this.busy[id] = false;
                    this.schedule();
                }
            },
            async readSettings(id) {
                if (this.busy[id] || !this.accounts[id] || this.sessionExpired) return;
                this.busy[id] = true;
                const controller = new AbortController();
                formControllers.add(controller);
                const timeout = setTimeout(() => controller.abort(), 15000);
                try {
                    const response = await fetch(this.config.status_url, {credentials:'same-origin', cache:'no-store',
                        headers:{Accept:'application/json'}, signal:controller.signal});
                    const data = await read(response);
                    const current = (data.accounts || []).find(account => account.id === id && account.marketplace_code === 'ozon');
                    const form = document.getElementById('ozon-account-' + id)?.querySelector('form[data-account-settings]');
                    if (stopped) return;
                    if (!current || !Number.isSafeInteger(current.version) || current.version <= 0
                            || current.external_account_id !== form?.elements.namedItem('client_id')?.value) {
                        throw new Error('Не удалось подтвердить этот магазин. Ваши значения сохранены в форме.');
                    }
                    revision += 1;
                    this.apply(data);
                    this.settingsReview[id] = {version:current.version, label:current.label,
                        default_vat:current.settings?.default_vat || ''};
                    this.$nextTick?.(() => document.getElementById('ozon-settings-review-' + id)?.focus({preventScroll:true}));
                } catch (error) {
                    if (!stopped) {
                        this.sessionExpired = error.code === 'auth_required';
                        this.settingsErrors[id] = error.message || 'Не удалось перечитать настройки. Ваш ввод сохранён.';
                    }
                } finally {
                    clearTimeout(timeout); formControllers.delete(controller); this.busy[id] = false;
                }
            },
            acceptSettingsReview(id, useStored = false) {
                const reviewed = this.settingsReview[id];
                const form = document.getElementById('ozon-account-' + id)?.querySelector('form[data-account-settings]');
                if (!reviewed || !form || this.busy[id]) return;
                if (useStored) {
                    form.elements.namedItem('label').value = reviewed.label;
                    form.elements.namedItem('default_vat').value = {'0.10':'0.1','0.20':'0.2'}[reviewed.default_vat] || reviewed.default_vat;
                }
                form.elements.namedItem('expected_version').value = String(reviewed.version);
                this.settingsReviewed[id] = !useStored;
                this.settingsReview[id] = null;
                this.settingsConflicts[id] = false; this.settingsUncertain[id] = false; this.settingsErrors[id] = '';
                this.saved[id] = useStored ? 'В форму загружены сохранённые настройки.' : '';
                form.elements.namedItem('label')?.focus({preventScroll:true});
            },
            vatLabel(value) {
                return {'0':'Без НДС','0.05':'5%','0.07':'7%','0.1':'10%','0.10':'10%',
                    '0.2':'20%','0.20':'20%','0.22':'22%'}[value] || 'Не задан';
            },
            historyFor(id) { return this.history[id] || {items:[], loaded:false, busy:false, error:'', next:null}; },
            historyDate(value) {
                const observed = date(value);
                return observed && Number.isFinite(observed.getTime()) ? new Intl.DateTimeFormat('ru-RU',
                    {dateStyle:'medium', timeStyle:'short'}).format(observed) : 'Дата не указана';
            },
            async loadHistory(id, more = false, force = false) {
                if (!Number.isSafeInteger(id) || !this.accounts[id] || stopped || this.sessionExpired) return;
                const state = this.historyFor(id);
                if (!force && (state.busy || (state.loaded && !more))) return;
                if (more && !state.next) return;
                historyControllers.get(id)?.abort();
                const controller = new AbortController(); historyControllers.set(id, controller);
                this.history[id] = {...state, busy:true, error:''};
                const timeout = setTimeout(() => controller.abort(), 15000);
                try {
                    const url = new URL(this.config.accounts_url.replace(/\/?$/, '/') + id + '/history', location.origin);
                    if (url.origin !== location.origin) throw new Error('Не удалось открыть историю этого магазина.');
                    if (more) url.searchParams.set('before_id', String(state.next));
                    const data = await read(await fetch(url, {credentials:'same-origin', cache:'no-store',
                        headers:{Accept:'application/json'}, signal:controller.signal}));
                    if (stopped || historyControllers.get(id) !== controller) return;
                    if (data.account_id !== id || data.marketplace_code !== 'ozon' || !Array.isArray(data.items)
                            || data.items.length > 30 || data.items.some(item => !Number.isSafeInteger(item.id) || item.id <= 0)
                            || (data.next_before_id !== null && (!Number.isSafeInteger(data.next_before_id) || data.next_before_id <= 0))) {
                        throw new Error('Не удалось подтвердить историю этого магазина. Предыдущие записи сохранены.');
                    }
                    const items = more ? state.items.concat(data.items) : data.items;
                    if (new Set(items.map(item => item.id)).size !== items.length) throw new Error('История изменилась. Обновите список.');
                    this.history[id] = {items, loaded:true, busy:false, error:'', next:data.next_before_id};
                } catch (error) {
                    if (!stopped && historyControllers.get(id) === controller) {
                        this.sessionExpired = error.code === 'auth_required';
                        this.history[id] = {...state, busy:false, error: error.code === 'auth_required' ? error.message :
                            'Не удалось загрузить историю. Уже показанные записи сохранены.'};
                    }
                } finally {
                    clearTimeout(timeout);
                    if (historyControllers.get(id) === controller) historyControllers.delete(id);
                }
            },
            active(id) { return active(this.jobs[id]); },
            anyActive() { return Object.values(this.jobs).some(active); },
            connected(id) {
                const account = this.accounts[id] || {};
                const expiry = date(account.credential_expires_at);
                return account.is_active && account.has_credentials && account.connection_status === 'connected'
                    && (!expiry || expiry > new Date());
            },
            connectionLabel(id) {
                const account = this.accounts[id] || {};
                if (!account.is_active || !account.has_credentials) return 'Отключён';
                const expiry = date(account.credential_expires_at);
                if (expiry && expiry <= new Date()) return 'Срок ключа истёк';
                return {connected: 'Подключён', invalid: 'Ключ отклонён', error: 'Нужна проверка',
                    limited: 'Ограниченный доступ', unchecked: 'Ожидает проверки'}[account.connection_status] || 'Не проверен';
            },
            tone(id) {
                if ((this.accounts[id] || {}).connection_status === 'invalid') return 'sh-badge--red';
                return this.connected(id) ? 'sh-badge--green' : 'sh-badge--yellow';
            },
            step(id) {
                if (!this.connected(id)) return 0;
                if (this.active(id)) return 1;
                const sync = this.syncs[id];
                return sync && sync.status === 'completed' ? 2 : 1;
            },
            message(id) {
                const account = this.accounts[id] || {};
                if (!account.is_active || !account.has_credentials) return 'Кабинет отключён. Каталог и история сохранены. Для новой загрузки подключите ключ снова.';
                const expiry = date(account.credential_expires_at);
                if (expiry && expiry <= new Date()) return 'Срок ключа истёк. Замените ключ в настройках этого кабинета; сохранённые товары доступны.';
                const job = this.jobs[id];
                if (job) return job.message || 'Обновляем статус загрузки…';
                if (!this.connected(id)) return 'Проверьте подключение — после этого каталог загрузится автоматически.';
                const sync = this.syncs[id];
                if (sync && sync.status === 'completed') {
                    return sync.seen_count ? 'Каталог загружен. Можно перейти к товарам.' : 'На Ozon пока нет товаров. Можно подготовить первую карточку.';
                }
                return 'Подключение проверено. Загрузите существующие товары из Ozon.';
            },
            count(id) {
                const job = this.jobs[id];
                const sync = this.syncs[id];
                return job && (this.active(id) || job.status === 'completed') ? job.processed : (sync ? sync.seen_count : 0);
            },
            retryTime(id) {
                const value = date((this.jobs[id] || {}).next_retry_at);
                return value && !Number.isNaN(value.getTime())
                    ? 'Следующая попытка: ' + value.toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'}) : '';
            },
            async reviewCurrent(id) {
                if (!Number.isSafeInteger(id) || !this.accounts[id] || this.busy[id]) return;
                this.busy[id] = true;
                const controller = new AbortController();
                formControllers.add(controller);
                const timeout = setTimeout(() => controller.abort(), 15000);
                try {
                    const response = await fetch(this.config.status_url, {credentials:'same-origin', cache:'no-store',
                        headers:{Accept:'application/json'}, signal:controller.signal});
                    const data = await read(response);
                    const current = (data.accounts || []).find(account => account.id === id && account.marketplace_code === 'ozon');
                    const form = document.getElementById('ozon-account-' + id)?.querySelector('form[data-key-replacement]');
                    if (stopped) return;
                    if (!current || !Number.isSafeInteger(current.version) || current.version <= 0 || !form
                            || current.external_account_id !== form.elements.namedItem('client_id')?.value) {
                        throw new Error('Не удалось подтвердить тот же магазин. Новый ключ не отправлен.');
                    }
                    revision += 1;
                    this.apply(data);
                    form.elements.namedItem('expected_version').value = String(current.version);
                    form.elements.namedItem('label').value = current.label;
                    this.conflicts[id] = false;
                    this.errors[id] = '';
                    this.reviewed[id] = true;
                    this.openKey(id);
                } catch (error) {
                    if (!stopped) this.errors[id] = error.name === 'AbortError' || error instanceof TypeError
                        ? 'Статус не удалось перечитать. Введённый ключ остаётся в поле; ничего не отправлено.' : error.message;
                } finally {
                    clearTimeout(timeout);formControllers.delete(controller);this.busy[id] = false;this.schedule();
                }
            },
            expiry(id) {
                return (this.accounts[id] || {}).credential_expiry || {
                    state: 'unknown', needs_attention: false, expires_at: null,
                    label: 'Срок ключа не подтверждён', hint: 'Дата окончания не получена. Это не означает бессрочный доступ.'
                };
            },
            expiryDate(id) {
                const value = date(this.expiry(id).expires_at);
                return value && Number.isFinite(value.getTime())
                    ? new Intl.DateTimeFormat('ru-RU', {day:'numeric', month:'long', year:'numeric', hour:'2-digit', minute:'2-digit', timeZone:'UTC', timeZoneName:'short'}).format(value)
                    : '';
            },
            openKey(id, focusInput = true) {
                if (!Number.isSafeInteger(id) || !this.accounts[id] || !this.enabled) return false;
                const card = document.getElementById('ozon-account-' + id);
                const settings = card?.querySelector('[data-key-settings]');
                const input = settings?.querySelector('form[data-key-replacement] input[name="api_key"]');
                if (!settings || !input) return false;
                settings.open = true;
                this.keyOpen[id] = true;
                const focus = focusInput ? input : settings.querySelector('summary');
                focus?.focus({preventScroll:true});
                focus?.scrollIntoView({block:'center', behavior:'auto'});
                const target = new URL(location.href);
                target.searchParams.set('account_id', String(id));
                target.searchParams.set('action', 'replace-key');
                target.hash = 'ozon-account-' + id;
                history.replaceState(history.state, '', target);
                return true;
            },
            canWrite(id) { return ((this.accounts[id] || {}).capabilities || []).includes('catalog_write'); }
        };
    };
})();
