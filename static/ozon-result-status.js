/* Live result notice. Reads never reload the document or discard a repair form. */
(function (global) {
    'use strict';

    const MAX_SOURCE_PREPARE_HTML_BYTES = 4 * 1024 * 1024;
    const SOURCE_PREPARE_JOB_UID = /^ozon-upload-[a-f0-9]{32}$/;
    const FORBIDDEN_REGION_NODES = [
        'script', 'style', 'link', 'img', 'iframe', 'object', 'embed',
        'video', 'audio', 'source', 'track', 'base', 'meta',
    ].join(',');

    function isPositiveSafeInteger(value) {
        return Number.isSafeInteger(value) && value > 0;
    }

    function canonicalSamePageUrl(value) {
        if (typeof value !== 'string' || !value || value.length > 2048 ||
            !global.location || typeof global.location.href !== 'string') return null;
        try {
            const current = new URL(global.location.href);
            const target = new URL(value, current.href);
            if (!['http:', 'https:'].includes(current.protocol) ||
                target.protocol !== current.protocol || target.origin !== current.origin ||
                target.pathname !== current.pathname || current.search || target.search ||
                target.hash || target.username || target.password) return null;
            return target.href;
        } catch (_) {
            return null;
        }
    }

    function sourcePrepareRunState(run, expected) {
        if (!run || typeof run !== 'object' || Array.isArray(run) ||
            run.job_uid !== expected.jobUid || run.account_id !== expected.accountId ||
            run.mode !== 'source_prepare') return null;
        const summary = run.summary;
        if (!summary || typeof summary !== 'object' || Array.isArray(summary) ||
            summary.version !== 3 || summary.mode !== 'source_prepare' ||
            summary.account_id !== expected.accountId || summary.job_uid !== expected.jobUid ||
            !Number.isSafeInteger(summary.total) || summary.total !== expected.total ||
            !Number.isSafeInteger(summary.processed) || summary.processed < 0 ||
            !Number.isSafeInteger(summary.active) || summary.active < 0 ||
            summary.processed + summary.active !== summary.total) return null;

        if (typeof run.source_prepare_complete !== 'boolean') return null;
        if (run.status === 'running') {
            return summary.active > 0 && run.source_prepare_complete === false
                ? 'running' : null;
        }
        if (run.status !== 'completed' || summary.active !== 0 ||
            summary.processed !== summary.total) return null;
        return run.source_prepare_complete === true ? 'terminal' : 'awaiting_terminal';
    }

    function sourcePrepareRegions(root, expected, complete) {
        if (!root || typeof root.querySelectorAll !== 'function') return null;
        const matches = Array.from(root.querySelectorAll('[data-source-prepare-region]'));
        if (matches.length !== 2) return null;
        const regions = {};
        for (const node of matches) {
            const name = node.getAttribute('data-source-prepare-region');
            if (!['header', 'body'].includes(name) || regions[name]) return null;
            if (node.getAttribute('data-source-prepare-job') !== expected.jobUid ||
                node.getAttribute('data-source-prepare-account') !== String(expected.accountId) ||
                node.getAttribute('data-source-prepare-complete') !== complete) return null;
            regions[name] = node;
        }
        return regions.header && regions.body ? regions : null;
    }

    function hasUnsafeRegionContent(region) {
        if (!region || (region.matches && region.matches(FORBIDDEN_REGION_NODES)) ||
            region.querySelector(FORBIDDEN_REGION_NODES)) return true;
        const nodes = [region].concat(Array.from(region.querySelectorAll('*')));
        for (const node of nodes) {
            for (const attribute of Array.from(node.attributes || [])) {
                const name = attribute.name.toLowerCase();
                const value = String(attribute.value || '').trim();
                if (name.startsWith('on') || name === 'srcdoc') return true;
                if (['href', 'action', 'formaction', 'xlink:href'].includes(name)) {
                    if (/^(?:javascript|data):/i.test(value)) return true;
                    try {
                        const parsed = new URL(value, global.location.href);
                        if (!['http:', 'https:'].includes(parsed.protocol) ||
                            parsed.origin !== global.location.origin) return true;
                    } catch (_) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    function boundedHtmlText(response) {
        const declared = Number(response.headers.get('content-length'));
        if (Number.isFinite(declared) && declared > MAX_SOURCE_PREPARE_HTML_BYTES) {
            return Promise.reject(new Error('result_too_large'));
        }
        const reader = response.body && response.body.getReader
            ? response.body.getReader() : null;
        if (!reader) {
            return response.text().then(text => {
                if (new TextEncoder().encode(text).byteLength > MAX_SOURCE_PREPARE_HTML_BYTES) {
                    throw new Error('result_too_large');
                }
                return text;
            });
        }
        return (async () => {
            const chunks = [];
            let byteLength = 0;
            try {
                while (true) {
                    const part = await reader.read();
                    if (part.done) break;
                    byteLength += part.value.byteLength;
                    if (byteLength > MAX_SOURCE_PREPARE_HTML_BYTES) {
                        await reader.cancel();
                        throw new Error('result_too_large');
                    }
                    chunks.push(part.value);
                }
            } finally {
                if (reader.releaseLock) reader.releaseLock();
            }
            const bytes = new Uint8Array(byteLength);
            let offset = 0;
            for (const chunk of chunks) {
                bytes.set(chunk, offset);
                offset += chunk.byteLength;
            }
            return new TextDecoder('utf-8', {fatal: true}).decode(bytes);
        })();
    }

    function terminalRegionsFromHtml(html, expected) {
        const template = global.document.createElement('template');
        template.innerHTML = html;
        const regions = sourcePrepareRegions(template.content, expected, '1');
        if (!regions || hasUnsafeRegionContent(regions.header) ||
            hasUnsafeRegionContent(regions.body)) return null;
        return regions;
    }

    function regionsBusy(regions) {
        const active = global.document.activeElement;
        if (active && (regions.header.contains(active) || regions.body.contains(active))) {
            return true;
        }
        const refreshFallback = global.document.querySelector(
            '[data-source-prepare-refresh-fallback]',
        );
        if (active && refreshFallback &&
            (active === refreshFallback || refreshFallback.contains(active))) return true;
        for (const region of [regions.header, regions.body]) {
            const forms = Array.from(region.querySelectorAll('form'));
            if (forms.some(form => form.hasAttribute('data-source-prepare-action-pending'))) return true;
        }
        return false;
    }

    function createOptions(config) {
        let timer, controller, terminalController, terminalTimeout;
        let stopped = false, visibility, focusOut, submitCapture, focusOutTimer;
        let terminalHtmlRequested = false, pendingTerminalRegions = null, mountedView = null;
        const initial = config.initial;
        const isSourcePrepareRun = config.kind === 'run' && initial &&
            initial.mode === 'source_prepare' &&
            typeof initial.job_uid === 'string' && SOURCE_PREPARE_JOB_UID.test(initial.job_uid) &&
            isPositiveSafeInteger(initial.account_id);
        const expected = isSourcePrepareRun && initial.summary &&
            Number.isSafeInteger(initial.summary.total) && initial.summary.total > 0
            ? {jobUid: initial.job_uid, accountId: initial.account_id, total: initial.summary.total}
            : null;
        const sourcePrepareHandoff = Boolean(
            expected && initial.source_prepare_complete === false &&
            sourcePrepareRunState(initial, expected) &&
            canonicalSamePageUrl(config.terminal_html_url),
        );
        const signature = value => JSON.stringify([
            value.status, value.version, value.summary, value.item_results,
            value.error_code, value.error_message, value.source_prepare_complete,
        ]);
        const initialSignature = signature(initial);

        function stopSourcePrepareForMismatch(view) {
            stopped = true;
            view.changed = true;
            view.error = 'Не удалось подтвердить область подготовки. Откройте итог вручную.';
            view.failures = 0;
        }

        function applyPendingTerminalRegions(view) {
            if (!pendingTerminalRegions || stopped) return false;
            const current = sourcePrepareRegions(global.document, expected, '0');
            const pending = pendingTerminalRegions;
            if (!current || current.header !== pending.oldHeader || current.body !== pending.oldBody) {
                pendingTerminalRegions = null;
                view.error = 'Не удалось подтвердить обновлённый итог. Откройте его вручную.';
                view.sourcePrepareSwapDeferred = false;
                return false;
            }
            if (regionsBusy(current)) {
                view.sourcePrepareSwapDeferred = true;
                return false;
            }
            const currentProducts = current.header.querySelectorAll(
                'a[data-source-prepare-products]',
            );
            const newProducts = pending.header.querySelectorAll(
                '[data-source-prepare-products-slot] a[data-source-prepare-products]',
            );
            const productSlot = pending.header.querySelectorAll(
                '[data-source-prepare-products-slot]',
            );
            const currentDetails = current.body.querySelectorAll(
                'details.preparation-journey-path',
            );
            const newDetails = pending.body.querySelectorAll(
                'details.preparation-journey-path',
            );
            if (currentProducts.length !== 1 || newProducts.length !== 1 ||
                productSlot.length !== 1 || currentDetails.length !== 1 || newDetails.length !== 1) {
                pendingTerminalRegions = null;
                view.error = 'Не удалось подтвердить обновлённый итог. Откройте его вручную.';
                view.sourcePrepareSwapDeferred = false;
                return false;
            }
            newDetails[0].open = currentDetails[0].open;
            newProducts[0].replaceWith(currentProducts[0]);
            current.header.replaceWith(pending.header);
            current.body.replaceWith(pending.body);
            pendingTerminalRegions = null;
            view.sourcePrepareSwapDeferred = false;
            view.sourcePrepareUpdated = true;
            view.changed = false;
            view.error = '';
            return true;
        }

        async function fetchTerminalRegions(view) {
            if (!sourcePrepareHandoff || terminalHtmlRequested || stopped) return;
            const target = canonicalSamePageUrl(config.terminal_html_url);
            const oldRegions = sourcePrepareRegions(global.document, expected, '0');
            if (!target || !oldRegions || hasUnsafeRegionContent(oldRegions.header) ||
                hasUnsafeRegionContent(oldRegions.body)) {
                view.error = 'Не удалось подтвердить обновлённый итог. Откройте его вручную.';
                return;
            }
            terminalHtmlRequested = true;
            terminalController = new AbortController();
            terminalTimeout = setTimeout(() => terminalController.abort(), 10000);
            try {
                const response = await fetch(target, {
                    signal: terminalController.signal,
                    credentials: 'same-origin',
                    cache: 'no-store',
                    redirect: 'error',
                    headers: {Accept: 'text/html'},
                });
                if (stopped) return;
                if (response.status === 401) {
                    throw new Error('Сессия завершилась. Откройте итог после входа.');
                }
                if (!response.ok || response.redirected ||
                    !(response.headers.get('content-type') || '').includes('text/html')) {
                    throw new Error('result_unavailable');
                }
                const finalUrl = canonicalSamePageUrl(response.url);
                if (finalUrl !== target) throw new Error('result_url_mismatch');
                const html = await boundedHtmlText(response);
                if (stopped) return;
                const terminal = terminalRegionsFromHtml(html, expected);
                if (!terminal) throw new Error('result_scope_mismatch');
                pendingTerminalRegions = {
                    header: terminal.header,
                    body: terminal.body,
                    oldHeader: oldRegions.header,
                    oldBody: oldRegions.body,
                };
                if (!applyPendingTerminalRegions(view)) {
                    if (!pendingTerminalRegions) return;
                    view.sourcePrepareSwapDeferred = true;
                }
            } catch (error) {
                if (!stopped) {
                    view.error = error.message === 'Сессия завершилась. Откройте итог после входа.'
                        ? error.message
                        : 'Не удалось обновить итог автоматически. Откройте его вручную.';
                }
            } finally {
                clearTimeout(terminalTimeout);
                terminalTimeout = null;
                terminalController = null;
            }
        }

        return {
            data: () => ({
                current: initial, changed: false, error: '', busy: false, failures: 0,
                sourcePrepareUpdated: false, sourcePrepareSwapDeferred: false,
            }),
            computed: {
                active() {
                    if (config.kind === 'run') {
                        if (sourcePrepareHandoff) {
                            const state = sourcePrepareRunState(this.current, expected);
                            return state === 'running' || state === 'awaiting_terminal';
                        }
                        return this.current.status === 'running';
                    }
                    return ['queued', 'submitting', 'submitted', 'polling', 'uncertain'].includes(this.current.status) && !!this.current.next_poll_at;
                },
                sourcePrepareAwaitingTerminal() {
                    return sourcePrepareHandoff &&
                        sourcePrepareRunState(this.current, expected) === 'awaiting_terminal';
                },
                automaticStopped() { return config.kind === 'operation' && this.current.status === 'uncertain' && !this.current.next_poll_at; },
                cooldownLabel() {
                    if (config.kind !== 'operation' || !['queued', 'submitting', 'submitted', 'polling', 'uncertain'].includes(this.current.status)) return '';
                    const raw = this.current.request_summary?.provider_read_not_before;
                    if (typeof raw !== 'string') return '';
                    const due = new Date(/[Zz]|[+-]\d\d:\d\d$/.test(raw) ? raw : raw + 'Z');
                    if (!Number.isFinite(due.getTime()) || due.getTime() <= Date.now()) return '';
                    if (due.getUTCFullYear() > 9998) return 'Ozon сообщил слишком долгую паузу. Нужна проверка доступа к API.';
                    return 'Ozon попросил паузу. Новая проверка доступна не раньше ' + due.toLocaleString('ru-RU') + ' (ваше время).';
                },
                label() {
                    if (this.sourcePrepareAwaitingTerminal) return 'Ждём подтверждения завершения подготовки';
                    if (config.kind === 'run') return {running: 'Выполняется', success: 'Завершена', partial: 'Завершена частично', attention: 'Нужно ваше внимание'}[this.current.summary?.outcome] || 'Результат обновлён';
                    return {queued: 'В очереди', submitting: 'Отправляется', submitted: 'Ozon принял задачу', polling: 'Проверяем результат', uncertain: 'Нужна сверка', succeeded: 'Выполнено', partial: 'Выполнено частично', failed: 'Не выполнено', cancelled: 'Остановлено'}[this.current.status] || 'Результат обновлён';
                },
            },
            methods: {
                schedule() {
                    clearTimeout(timer);
                    if (!stopped && this.active && !global.document.hidden) timer = setTimeout(() => this.refresh(), Math.min(30000, 5000 * (1 + this.failures)));
                },
                async refresh() {
                    if (stopped || this.busy || global.document.hidden) return;
                    this.busy = true;
                    controller = new AbortController();
                    const timeout = setTimeout(() => controller.abort(), 10000);
                    try {
                        const response = await fetch(config.url, {
                            signal: controller.signal,
                            credentials: 'same-origin',
                            headers: {Accept: 'application/json'},
                        });
                        if (response.status === 401 || response.redirected) {
                            stopped = true;
                            if (sourcePrepareHandoff) this.changed = true;
                            throw new Error('Войдите снова, чтобы проверить результат. Введённые данные остаются на странице.');
                        }
                        if (!response.ok || !(response.headers.get('content-type') || '').includes('application/json')) {
                            throw new Error('Не удалось обновить статус. Сохранённый результат доступен ниже.');
                        }
                        let body;
                        try {
                            body = await response.json();
                        } catch (error) {
                            if (sourcePrepareHandoff) {
                                stopSourcePrepareForMismatch(this);
                                return;
                            }
                            throw error;
                        }
                        const value = body && typeof body === 'object' && !Array.isArray(body)
                            ? body[config.kind] : null;
                        if (!body || body.success === false || !value || typeof value.status !== 'string') {
                            if (sourcePrepareHandoff) {
                                stopSourcePrepareForMismatch(this);
                                return;
                            }
                            throw new Error('Не удалось подтвердить новый статус. Повторите проверку позже.');
                        }
                        if (stopped) return;
                        let sourceState = null;
                        if (sourcePrepareHandoff) {
                            sourceState = sourcePrepareRunState(value, expected);
                            if (!sourceState) {
                                stopSourcePrepareForMismatch(this);
                                return;
                            }
                        }
                        this.current = value;
                        this.changed = sourceState === 'running' || sourceState === 'awaiting_terminal'
                            ? false : signature(value) !== initialSignature;
                        this.sourcePrepareUpdated = false;
                        this.sourcePrepareSwapDeferred = false;
                        this.error = '';
                        this.failures = 0;
                        if (sourceState === 'terminal') await fetchTerminalRegions(this);
                    } catch (error) {
                        if (!global.document.hidden) {
                            this.error = error instanceof TypeError || error.name === 'AbortError'
                                ? 'Соединение прервано. Сохранённый результат доступен ниже.'
                                : error.message;
                        }
                        this.failures += 1;
                    } finally {
                        clearTimeout(timeout);
                        this.busy = false;
                        this.schedule();
                    }
                },
            },
            mounted() {
                mountedView = this;
                visibility = () => {
                    clearTimeout(timer);
                    if (global.document.hidden) controller?.abort();
                    else this.schedule();
                };
                focusOut = () => {
                    if (!pendingTerminalRegions) return;
                    clearTimeout(focusOutTimer);
                    focusOutTimer = setTimeout(() => applyPendingTerminalRegions(mountedView), 0);
                };
                submitCapture = event => {
                    const form = event.target;
                    if (form && form.closest && form.closest('[data-source-prepare-region]')) {
                        form.setAttribute('data-source-prepare-action-pending', 'true');
                    }
                };
                global.document.addEventListener('visibilitychange', visibility);
                if (sourcePrepareHandoff) {
                    global.document.addEventListener('focusout', focusOut, true);
                    global.document.addEventListener('submit', submitCapture, true);
                }
                this.schedule();
            },
            beforeUnmount() {
                stopped = true;
                clearTimeout(timer);
                clearTimeout(focusOutTimer);
                controller?.abort();
                terminalController?.abort();
                clearTimeout(terminalTimeout);
                global.document.removeEventListener('visibilitychange', visibility);
                if (sourcePrepareHandoff) {
                    global.document.removeEventListener('focusout', focusOut, true);
                    global.document.removeEventListener('submit', submitCapture, true);
                }
                pendingTerminalRegions = null;
                mountedView = null;
            },
        };
    }

    global.ozonResultStatus = {createOptions};
    const bootstrap = global.document.getElementById('ozon-result-bootstrap');
    if (bootstrap && global.Vue) {
        global.Vue.createApp(createOptions(JSON.parse(bootstrap.textContent))).mount('#ozon-result-status');
    }
})(window);
