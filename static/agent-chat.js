(function () {
    'use strict';

    function readJson(id, fallback) {
        const node = document.getElementById(id);
        if (!node) return fallback;
        try { return JSON.parse(node.textContent); } catch (_) { return fallback; }
    }

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    window.unifiedAgentChat = function () {
        const initial = readJson('agent-chat-initial', {});
        return {
            conversations: readJson('agent-chat-conversations', []),
            conversation: initial.conversation || {},
            messages: initial.messages || [],
            run: initial.run || null,
            runSteps: initial.steps || [],
            subtasks: initial.subtasks || [],
            proposals: initial.proposals || [],
            lastStepId: initial.last_step_id || 0,
            runtime: readJson('agent-chat-runtime', { online: false, unavailable: [] }),
            modelPolicy: readJson('agent-chat-model-policy', {
                single_model: true,
                primary_model: 'deepseek-v4-pro',
                fast_model: 'deepseek-v4-pro',
            }),
            activeScope: initial.active_scope || null,
            draft: '',
            productIds: [],
            productIdsInput: '',
            selectedEntityKind: 'imported_product',
            scopeMode: 'all',
            scopeOpen: false,
            historyOpen: false,
            inspectorOpen: window.innerWidth >= 1280,
            focused: false,
            sending: false,
            actionBusy: false,
            refreshing: false,
            error: '',
            errorKind: 'error',
            undoTaskId: null,
            pollTimer: null,
            userNearBottom: true,
            artifactExpanded: {},
            artifactSelections: {},

            get busy() { return this.sending || this.actionBusy; },
            get visibleSteps() { return this.runSteps.slice(-40); },
            get pendingProposalCount() { return this.proposals.filter(item => item.status === 'pending').length; },
            pendingProposalCountFor(message) {
                return this.run && message && message.task_id === this.run.id
                    ? this.pendingProposalCount
                    : 0;
            },

            init() {
                this.normalizePayload();
                this.hydrateScope(this.activeScope);
                this.$nextTick(() => this.scrollToBottom(false));
                this.schedulePoll(this.isRunActive() ? 1400 : 0);
                document.addEventListener('visibilitychange', () => {
                    if (document.hidden) {
                        clearTimeout(this.pollTimer);
                    } else {
                        this.refresh(true);
                    }
                });
            },

            normalizePayload() {
                this.messages = (this.messages || []).map(message => ({
                    ...message,
                    metadata: message.metadata || {},
                }));
                this.runSteps = this.runSteps || [];
                this.subtasks = this.subtasks || [];
            },

            async api(path, options) {
                const config = { ...(options || {}) };
                config.headers = {
                    'Accept': 'application/json',
                    'X-CSRFToken': csrfToken(),
                    ...(config.headers || {}),
                };
                if (config.body && !config.headers['Content-Type']) {
                    config.headers['Content-Type'] = 'application/json';
                }
                const response = await fetch(path, config);
                let data = {};
                try { data = await response.json(); } catch (_) { data = {}; }
                if (!response.ok) throw new Error(data.error || `Ошибка запроса (${response.status})`);
                return data;
            },

            setBanner(text, kind) {
                this.error = text || '';
                this.errorKind = kind || 'error';
                if (kind === 'success') setTimeout(() => {
                    if (this.error === text) this.error = '';
                }, 4500);
            },

            closePanels() {
                this.historyOpen = false;
                if (window.innerWidth < 1280) this.inspectorOpen = false;
                this.scopeOpen = false;
            },

            async newConversation() {
                if (this.busy) return;
                this.actionBusy = true;
                this.error = '';
                try {
                    const data = await this.api('/agents/api/conversations', {
                        method: 'POST', body: JSON.stringify({}),
                    });
                    this.conversations.unshift(data.conversation);
                    this.conversation = data.conversation;
                    this.messages = [];
                    this.run = null;
                    this.runSteps = [];
                    this.subtasks = [];
                    this.proposals = [];
                    this.lastStepId = 0;
                    this.resetScope();
                    this.historyOpen = false;
                    history.replaceState(null, '', `/agents?conversation=${data.conversation.id}`);
                    this.$nextTick(() => this.$refs.composer && this.$refs.composer.focus());
                } catch (error) {
                    this.setBanner(error.message);
                } finally {
                    this.actionBusy = false;
                }
            },

            async openConversation(id) {
                if (!id || id === this.conversation.id || this.busy) {
                    this.historyOpen = false;
                    return;
                }
                this.actionBusy = true;
                this.error = '';
                try {
                    const payload = await this.api(`/agents/api/conversations/${id}`);
                    this.applyPayload(payload, true);
                    this.schedulePoll(this.isRunActive() ? 1400 : 0);
                    this.historyOpen = false;
                    history.replaceState(null, '', `/agents?conversation=${id}`);
                } catch (error) {
                    this.setBanner(error.message);
                } finally {
                    this.actionBusy = false;
                }
            },

            useSuggestion(text) {
                this.draft = text;
                this.$nextTick(() => {
                    this.resizeComposer();
                    this.$refs.composer && this.$refs.composer.focus();
                });
            },

            resizeComposer() {
                const field = this.$refs.composer;
                if (!field) return;
                field.style.height = 'auto';
                field.style.height = `${Math.min(field.scrollHeight, 150)}px`;
            },

            handleEnter(event) {
                if (event.isComposing || event.shiftKey) return;
                event.preventDefault();
                this.sendMessage();
            },

            applyScope() {
                if (this.scopeMode === 'all') {
                    this.resetScope();
                } else {
                    const matches = this.productIdsInput.match(/\d+/g) || [];
                    this.productIds = Array.from(new Set(matches.map(Number).filter(id => id > 0))).slice(0, 500);
                    this.productIdsInput = this.productIds.join(', ');
                    this.selectedEntityKind = 'imported_product';
                    if (!this.productIds.length) {
                        this.setBanner('Укажите хотя бы один корректный ID товара');
                        return;
                    }
                }
                this.scopeOpen = false;
            },

            resetScope() {
                this.productIds = [];
                this.productIdsInput = '';
                this.scopeMode = 'all';
                this.selectedEntityKind = 'imported_product';
            },

            hydrateScope(scope) {
                const ids = Array.isArray((scope || {}).ids)
                    ? scope.ids.map(Number).filter(id => Number.isSafeInteger(id) && id > 0).slice(0, 500)
                    : [];
                if (!ids.length) {
                    this.resetScope();
                    return;
                }
                this.productIds = Array.from(new Set(ids));
                this.productIdsInput = this.productIds.join(', ');
                this.selectedEntityKind = this.normalizedEntityKind(scope.kind);
                this.scopeMode = 'selected';
            },

            async sendMessage() {
                const text = this.draft.trim();
                if (!text || this.busy) return;
                const optimisticId = `pending-${Date.now()}`;
                const optimistic = {
                    id: optimisticId,
                    role: 'user', kind: 'text', content: text,
                    metadata: {
                        product_ids: this.productIds.slice(),
                        entity_scope: {
                            kind: this.selectedEntityKind,
                            ids: this.productIds.slice(),
                        },
                    },
                    created_at: new Date().toISOString(),
                };
                this.messages.push(optimistic);
                this.draft = '';
                this.sending = true;
                this.error = '';
                this.$nextTick(() => {
                    this.resizeComposer();
                    this.scrollToBottom(true);
                });
                try {
                    const data = await this.api(
                        `/agents/api/conversations/${this.conversation.id}/messages`,
                        {
                            method: 'POST',
                            body: JSON.stringify({
                                message: text,
                                product_ids: this.productIds,
                                entity_kind: this.productIds.length ? this.selectedEntityKind : null,
                                scope_mode: this.productIds.length ? 'selected' : 'global',
                            }),
                        },
                    );
                    this.messages = this.messages.filter(message => message.id !== optimisticId);
                    for (const message of data.messages || []) this.upsertMessage(message);
                    this.conversation = data.conversation || this.conversation;
                    this.upsertConversation(this.conversation);
                    await this.refresh(true);
                    this.$nextTick(() => this.scrollToBottom(true));
                } catch (error) {
                    this.messages = this.messages.filter(message => message.id !== optimisticId);
                    this.draft = text;
                    this.setBanner(error.message);
                    this.$nextTick(() => this.resizeComposer());
                } finally {
                    this.sending = false;
                }
            },

            async approvePlan(message) {
                if (this.busy) return;
                this.actionBusy = true;
                this.error = '';
                try {
                    await this.api(
                        `/agents/api/conversations/${this.conversation.id}/plans/${message.id}/approve`,
                        { method: 'POST', body: '{}' },
                    );
                    await this.refresh(true);
                    this.inspectorOpen = window.innerWidth >= 760;
                } catch (error) {
                    this.setBanner(error.message);
                } finally {
                    this.actionBusy = false;
                }
            },

            async rejectPlan(message) {
                if (this.busy) return;
                this.actionBusy = true;
                try {
                    await this.api(
                        `/agents/api/conversations/${this.conversation.id}/plans/${message.id}/reject`,
                        { method: 'POST', body: '{}' },
                    );
                    await this.refresh(true);
                } catch (error) {
                    this.setBanner(error.message);
                } finally {
                    this.actionBusy = false;
                }
            },

            async cancelRun(taskId) {
                if (!taskId || this.busy) return;
                this.actionBusy = true;
                try {
                    await this.api(`/agents/api/tasks/${taskId}/cancel`, { method: 'POST', body: '{}' });
                    await this.refresh(true);
                    this.setBanner('Задача остановлена', 'success');
                } catch (error) {
                    this.setBanner(error.message);
                } finally {
                    this.actionBusy = false;
                }
            },

            requestUndo(taskId) { this.undoTaskId = taskId; },

            async confirmUndo() {
                if (!this.undoTaskId || this.busy) return;
                const taskId = this.undoTaskId;
                this.actionBusy = true;
                try {
                    const data = await this.api(`/agents/api/tasks/${taskId}/rollback`, {
                        method: 'POST', body: '{}',
                    });
                    this.undoTaskId = null;
                    await this.refresh(true);
                    if (Number(data.conflicts || 0) > 0) {
                        this.setBanner(
                            `Откат выполнен частично: восстановлено — ${data.products}, `
                            + `конфликтов ручных изменений — ${data.conflicts}`,
                        );
                    } else {
                        this.setBanner(`Откат выполнен: восстановлено товаров — ${data.products}`, 'success');
                    }
                } catch (error) {
                    this.setBanner(error.message);
                } finally {
                    this.actionBusy = false;
                }
            },

            async reviewProposal(proposalId, decision) {
                if (this.busy) return;
                this.actionBusy = true;
                try {
                    await this.api(`/agents/api/proposals/${proposalId}/${decision}`, {
                        method: 'POST', body: '{}',
                    });
                    await this.refresh(true);
                    this.setBanner(
                        decision === 'apply' ? 'Предложение применено после ручной проверки' : 'Предложение отклонено',
                        'success',
                    );
                } catch (error) {
                    this.setBanner(error.message);
                } finally {
                    this.actionBusy = false;
                }
            },

            schedulePoll(delay) {
                clearTimeout(this.pollTimer);
                if (!delay || document.hidden) return;
                this.pollTimer = setTimeout(() => this.refresh(false), delay);
            },

            async refresh(force) {
                if (this.refreshing || !this.conversation.id || document.hidden) return;
                this.refreshing = true;
                try {
                    const payload = await this.api(
                        `/agents/api/conversations/${this.conversation.id}?step_after=${this.lastStepId}`,
                    );
                    this.applyPayload(payload, false);
                } catch (error) {
                    if (force) this.setBanner(error.message);
                } finally {
                    this.refreshing = false;
                    this.schedulePoll(this.isRunActive() ? 1400 : 0);
                }
            },

            applyPayload(payload, resetSteps) {
                const shouldScroll = this.userNearBottom;
                this.conversation = payload.conversation || this.conversation;
                this.messages = (payload.messages || []).map(message => ({
                    ...message, metadata: message.metadata || {},
                }));
                this.run = payload.run || null;
                this.subtasks = payload.subtasks || [];
                this.proposals = payload.proposals || [];
                this.activeScope = payload.active_scope || null;
                // Server-side grounding may resolve a typed card from an nmID
                // written in the message, and the semantic planner may also
                // explicitly switch back to global scope. Keep the composer in
                // sync so the next conversational turn cannot lose that state.
                if (resetSteps || !this.scopeOpen) this.hydrateScope(this.activeScope);
                if (resetSteps) {
                    this.runSteps = payload.steps || [];
                } else {
                    const merged = new Map(this.runSteps.map(step => [step.id, step]));
                    for (const step of payload.steps || []) merged.set(step.id, step);
                    this.runSteps = Array.from(merged.values()).sort((a, b) => a.id - b.id).slice(-200);
                }
                this.lastStepId = Math.max(this.lastStepId, payload.last_step_id || 0);
                this.upsertConversation(this.conversation);
                if (shouldScroll) this.$nextTick(() => this.scrollToBottom(false));
            },

            upsertMessage(message) {
                message.metadata = message.metadata || {};
                const index = this.messages.findIndex(item => item.id === message.id);
                if (index >= 0) this.messages.splice(index, 1, message);
                else this.messages.push(message);
                this.messages.sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
            },

            upsertConversation(conversation) {
                if (!conversation || !conversation.id) return;
                const index = this.conversations.findIndex(item => item.id === conversation.id);
                if (index >= 0) this.conversations.splice(index, 1, conversation);
                else this.conversations.unshift(conversation);
                this.conversations.sort((a, b) => new Date(b.last_message_at) - new Date(a.last_message_at));
            },

            trackScroll() {
                const node = this.$refs.messages;
                if (!node) return;
                this.userNearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 100;
            },

            scrollToBottom(smooth) {
                const node = this.$refs.messages;
                if (!node) return;
                node.scrollTo({ top: node.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
                this.userNearBottom = true;
            },

            isRunActive() { return this.run && ['queued', 'running'].includes(this.run.status); },
            runDisplayStatus(value) {
                const metadata = (value || {}).metadata || {};
                const taskStatus = metadata.status || (value || {}).status || 'queued';
                const result = metadata.result || (value || {}).result || {};
                if (taskStatus === 'completed' && result.status === 'partial') return 'partial';
                if (taskStatus === 'completed' && result.status === 'failed') return 'failed';
                return taskStatus;
            },
            runUsage(run) {
                return (((run || {}).result || {})._usage || {});
            },
            hasRunUsage(run) {
                return Object.prototype.hasOwnProperty.call(this.runUsage(run), 'api_requests');
            },
            runModelSummary(run) {
                const usage = this.runUsage(run);
                const requests = Number(usage.api_requests || 0);
                if (!requests) return 'Без LLM';
                const models = Object.keys(usage.models || {}).map(model => this.friendlyModel(model));
                const label = models.length ? models.join(' + ') : 'LLM';
                const suffix = requests === 1 ? '1 запрос' : `${requests} запросов`;
                return `${label} · ${suffix}`;
            },
            runTokenSummary(run) {
                return new Intl.NumberFormat('ru-RU').format(Number(this.runUsage(run).total_tokens || 0));
            },
            statusLabel(status) {
                return ({
                    queued: 'В очереди', running: 'Выполняется', completed: 'Завершено',
                    partial: 'Частично выполнено', failed: 'Ошибка',
                    cancelled: 'Остановлено', pending_approval: 'Нужно подтверждение',
                })[status] || 'Готово';
            },
            skillLabel(name) {
                return ({
                    'category-mapper': 'Категории WB', 'characteristics-filler': 'Характеристики',
                    'seo-writer': 'Контент и SEO', 'card-doctor': 'Модерация',
                    'brand-resolver': 'Бренды', 'size-normalizer': 'Размеры',
                    'price-optimizer': 'Ценообразование', 'review-analyst': 'Отзывы',
                    'photo-optimizer': 'Фотографии', 'supplier-audit': 'Аудит поставщика',
                    'image-generator': 'AI-фотостудия',
                    'batch-audit': 'Пакетный аудит', 'catalog-query': 'Поиск по каталогу', 'card-insight': 'Анализ карточки',
                    'content-writer': 'Редактор карточки', 'description-writer': 'Редактор описания',
                    'wb-content-publisher': 'Отправка в WB', 'system-query': 'Данные системы',
                })[name] || 'Внутренний навык';
            },
            runArtifacts(message) {
                const result = (message.metadata || {}).result || {};
                const artifacts = [];
                for (const [stepIndex, step] of (result.results || []).entries()) {
                    const stepResult = step.result || {};
                    const stepArtifacts = Array.isArray(stepResult.artifacts)
                        ? stepResult.artifacts.filter(item => item && typeof item === 'object')
                        : [];
                    const stepProducts = Array.isArray(stepResult.products)
                        ? stepResult.products.filter(item => item && typeof item === 'object')
                        : [];
                    const isContentResult = ['content-writer', 'description-writer'].includes(step.skill);
                    const isContentBatch = isContentResult
                        && (stepArtifacts.length > 1 || stepProducts.length > 1);
                    if (isContentBatch) {
                        const entityKind = this.normalizedEntityKind(
                            stepResult.entity_kind || (stepArtifacts[0] || {}).entity_kind,
                        );
                        const detailsById = new Map(stepArtifacts.map(item => [Number(item.id), item]));
                        const sourceItems = stepProducts.length ? stepProducts : stepArtifacts;
                        const items = sourceItems.slice(0, 100)
                            .map(item => {
                                const detail = detailsById.get(Number(item.id)) || {};
                                return this.collectionItem({
                                    ...item,
                                    changes: detail.changes || item.changes || {},
                                    change_fields: item.changed_fields || item.change_fields,
                                }, entityKind);
                            })
                            .filter(Boolean);
                        artifacts.push({
                            type: 'collection', collection_kind: 'changes', entity_kind: entityKind,
                            id: `${message.task_id || message.id}-${step.skill}-${stepIndex}-changes`,
                            title: 'Изменения контента',
                            total: Number(stepResult.saved || sourceItems.length),
                            truncated: sourceItems.length > items.length,
                            items,
                        });
                    } else {
                        artifacts.push(...stepArtifacts.map(item => ({
                            ...item,
                            title: this.decodeText(item.title || `Карточка #${item.id}`),
                        })));
                    }
                    if (stepProducts.length && !isContentResult) {
                        const entityKind = this.normalizedEntityKind(stepResult.entity_kind);
                        const items = stepProducts.slice(0, 100)
                            .map(product => this.collectionItem(product, entityKind))
                            .filter(Boolean);
                        artifacts.push({
                            type: 'collection',
                            collection_kind: step.skill === 'batch-audit' ? 'audit' : 'results',
                            entity_kind: entityKind,
                            id: `${message.task_id || message.id}-${step.skill || 'step'}-${stepIndex}`,
                            title: stepResult.collection_title || stepResult.condition || 'Результаты поиска',
                            total: Number(stepResult.total || stepProducts.length),
                            issue_total: Number(stepResult.cards_with_issues || 0),
                            truncated: Boolean(stepResult.truncated) || stepProducts.length > items.length,
                            items,
                        });
                    }
                }
                return artifacts.slice(0, 20);
            },
            imageArtifactStatusLabel(artifact) {
                if ((artifact || {}).has_final && (artifact || {}).status === 'completed') return 'Готово';
                return ({
                    queued: 'В очереди', running: 'Создаётся',
                    remote_running: 'Создаётся', finalizing: 'Собирается',
                    completed: 'Готово', failed: 'Ошибка', cancelled: 'Остановлено',
                })[(artifact || {}).status] || 'На проверке';
            },
            imageArtifactStatusClass(artifact) {
                if ((artifact || {}).has_final && (artifact || {}).status === 'completed') return 'is-ready';
                if ((artifact || {}).status === 'failed') return 'is-failed';
                return 'is-progress';
            },
            imageArtifactCostLabel(artifact) {
                const value = Number((artifact || {}).estimated_cost_rub);
                if (!Number.isFinite(value)) return 'Стоимость по тарифу';
                return `${new Intl.NumberFormat('ru-RU', {
                    minimumFractionDigits: 2, maximumFractionDigits: 2,
                }).format(value)} ₽`;
            },
            runFailures(message) {
                const result = ((message || {}).metadata || {}).result || {};
                const failures = [];
                for (const step of result.results || []) {
                    for (const item of ((step || {}).result || {}).failure_details || []) {
                        if (!item || typeof item !== 'object') continue;
                        failures.push({
                            product_id: Number(item.product_id || 0),
                            code: String(item.code || 'error'),
                            error: String(item.error || 'Не удалось сохранить карточку'),
                        });
                    }
                }
                return failures.slice(0, 20);
            },
            decodeText(value) {
                const node = document.createElement('textarea');
                node.innerHTML = String(value || '');
                return node.value;
            },
            normalizedEntityKind(value) {
                return value === 'product' ? 'product' : 'imported_product';
            },
            collectionItem(item, entityKind) {
                const id = Number(item.id);
                if (!Number.isSafeInteger(id) || id <= 0) return null;
                const kind = this.normalizedEntityKind(entityKind || item.entity_kind);
                const changes = item.changes && typeof item.changes === 'object' ? item.changes : {};
                return {
                    ...item,
                    id,
                    entity_kind: kind,
                    title: this.decodeText(item.title || `Карточка #${id}`),
                    url: item.url || (kind === 'product'
                        ? `/products/${id}`
                        : `/my-products/${id}/wb-preview`),
                    issue_labels: Array.isArray(item.issue_labels) ? item.issue_labels.slice(0, 5) : [],
                    change_fields: Array.isArray(item.change_fields)
                        ? item.change_fields.slice(0, 4)
                        : Object.keys(changes),
                };
            },
            collectionEyebrow(artifact) {
                const kind = artifact.entity_kind === 'product' ? 'Карточки WB' : 'Импортированные карточки';
                if (artifact.collection_kind === 'audit') return `Аудит · ${kind}`;
                if (artifact.collection_kind === 'changes') return `Изменения · ${kind}`;
                return `Подборка · ${kind}`;
            },
            collectionSummary(artifact) {
                if (artifact.collection_kind === 'audit') {
                    return `${Number(artifact.issue_total || artifact.items.length)} с проблемами из ${artifact.total}`;
                }
                if (artifact.collection_kind === 'changes') {
                    return `${artifact.total} изменено`;
                }
                const shown = (artifact.items || []).length;
                return artifact.truncated ? `${shown} показано из ${artifact.total}` : `${artifact.total} карточек`;
            },
            collectionTruncatedNote(artifact) {
                const shown = (artifact.items || []).length;
                if (artifact.collection_kind === 'audit') {
                    return `Показаны первые ${shown} карточек с проблемами`;
                }
                return `Показаны первые ${shown} из ${artifact.total}`;
            },
            artifactKey(artifact) { return String(artifact.id || `${artifact.type}-result`); },
            artifactIsExpanded(artifact) { return Boolean(this.artifactExpanded[this.artifactKey(artifact)]); },
            toggleArtifact(artifact) {
                const key = this.artifactKey(artifact);
                this.artifactExpanded = { ...this.artifactExpanded, [key]: !this.artifactExpanded[key] };
            },
            artifactSelectedIds(artifact) {
                return this.artifactSelections[this.artifactKey(artifact)] || [];
            },
            artifactAllItemsSelected(artifact) {
                const ids = (artifact.items || []).map(item => Number(item.id)).filter(id => id > 0);
                const selected = new Set(this.artifactSelectedIds(artifact));
                return ids.length > 0 && ids.every(id => selected.has(id));
            },
            artifactItemSelected(artifact, productId) {
                return this.artifactSelectedIds(artifact).includes(Number(productId));
            },
            toggleArtifactItem(artifact, productId) {
                const key = this.artifactKey(artifact);
                const id = Number(productId);
                const selected = new Set(this.artifactSelectedIds(artifact));
                if (selected.has(id)) selected.delete(id); else selected.add(id);
                this.artifactSelections = { ...this.artifactSelections, [key]: Array.from(selected).slice(0, 100) };
            },
            toggleAllArtifactItems(artifact) {
                const key = this.artifactKey(artifact);
                const ids = (artifact.items || []).map(item => Number(item.id)).filter(id => id > 0);
                this.artifactSelections = {
                    ...this.artifactSelections,
                    [key]: this.artifactAllItemsSelected(artifact) ? [] : ids.slice(0, 100),
                };
            },
            queueArtifactAction(artifact, action) {
                const ids = Array.from(new Set(
                    this.artifactSelectedIds(artifact).map(Number).filter(id => id > 0),
                ));
                if (!ids.length) {
                    this.setBanner('Сначала отметьте хотя бы одну карточку');
                    return;
                }
                this.productIds = ids.slice(0, 100);
                this.productIdsInput = this.productIds.join(', ');
                this.scopeMode = 'selected';
                this.selectedEntityKind = this.normalizedEntityKind(artifact.entity_kind);
                this.draft = ({
                    audit: 'Проведи аудит выбранных карточек и найди основные проблемы',
                    content: 'Улучши название и описание выбранных карточек',
                    prepare: 'Подготовь выбранные карточки к публикации на WB',
                })[action] || 'Продолжи работу с выбранными карточками';
                this.$nextTick(() => {
                    this.resizeComposer();
                    this.$refs.composer && this.$refs.composer.focus();
                    this.$refs.composer && this.$refs.composer.scrollIntoView({ behavior: 'smooth', block: 'center' });
                });
            },
            formatMoney(value) {
                if (value === null || value === undefined || value === '') return 'Цена не рассчитана';
                const number = Number(value);
                return Number.isFinite(number)
                    ? `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(number)} ₽`
                    : 'Цена не рассчитана';
            },
            artifactChanges(artifact) {
                return Object.entries(artifact.changes || {}).map(([field, value]) => ({
                    field,
                    old: value && typeof value === 'object' ? value.old : null,
                    new: value && typeof value === 'object' ? value.new : value,
                }));
            },
            compactValue(value) {
                if (value === null || value === undefined || value === '') return 'Не заполнено';
                const text = typeof value === 'string' ? value : JSON.stringify(value);
                return text.length > 220 ? `${text.slice(0, 220)}…` : text;
            },
            proposalChanges(proposal) {
                return Object.entries(proposal.changes || {}).map(([field, value]) => ({
                    field,
                    old: value && typeof value === 'object' ? value.old : null,
                    new: value && typeof value === 'object' ? value.new : value,
                }));
            },
            fieldLabel(field) {
                return ({
                    description: 'Описание', title: 'Название', brand: 'Бренд',
                    calculated_price: 'Расчётная цена',
                    calculated_discount_price: 'Цена со скидкой',
                    calculated_price_before_discount: 'Цена до скидки',
                    supplier_price: 'Закупочная цена',
                    supplier_quantity: 'Остаток', stock: 'Остаток', quantity: 'Остаток',
                })[field] || field;
            },
            formatValue(value) {
                if (value === null || value === undefined || value === '') return '—';
                return typeof value === 'number'
                    ? new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(value)
                    : String(value);
            },
            statusProposal(status) {
                return ({ applied: 'Применено', rejected: 'Отклонено', pending: 'Проверить' })[status] || status;
            },
            friendlyModel(model) {
                if (!model) return 'DeepSeek V4 Pro';
                return model.replace('deepseek-', 'DeepSeek ').replace('v4-', 'V4 ').replace(/\bpro\b/i, 'Pro').replace(/\bflash\b/i, 'Flash');
            },
            safeStepTitle(step) {
                if (step.step_type === 'thinking') return step.title || 'Анализ контекста';
                if (step.step_type === 'action') return step.title || 'Выполнение действия';
                if (step.step_type === 'decision') return step.title || 'Проверка результата';
                if (step.step_type === 'result') return step.title || 'Результат получен';
                if (step.step_type === 'error') return step.title || 'Ошибка шага';
                return step.title || 'Событие';
            },
            formatTime(value) {
                if (!value) return '';
                return new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' }).format(new Date(value));
            },
            formatDuration(seconds) {
                const value = Math.max(0, Number(seconds) || 0);
                if (value < 60) return `${value} сек`;
                const minutes = Math.floor(value / 60);
                const tail = value % 60;
                return tail ? `${minutes} мин ${tail} сек` : `${minutes} мин`;
            },
            formatRelativeDate(value) {
                if (!value) return '';
                const date = new Date(value);
                const now = new Date();
                const sameDay = date.toDateString() === now.toDateString();
                if (sameDay) return this.formatTime(value);
                return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: 'short' }).format(date);
            },
        };
    };
})();
