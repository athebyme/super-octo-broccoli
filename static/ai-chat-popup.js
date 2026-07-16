(function () {
    'use strict';

    const STORAGE_KEY = 'seller-hub-ai-popup-conversation';
    const SAFE_QUERY_KEYS = new Set(['page', 'tab', 'status', 'period', 'sort', 'order', 'conversation']);
    const ENTITY_ATTRIBUTES = [
        ['data-product-id', 'product_id'],
        ['data-seller-id', 'seller_id'],
        ['data-supplier-id', 'supplier_id'],
        ['data-item-id', 'item_id'],
        ['data-nm-id', 'nm_id'],
        ['data-subject-id', 'subject_id'],
        ['data-brand-id', 'brand_id'],
        ['data-marketplace-id', 'marketplace_id'],
        ['data-factory-id', 'factory_id'],
        ['data-account-id', 'account_id'],
        ['data-listing-id', 'listing_id'],
        ['data-marketplace-code', 'marketplace_code'],
        ['data-category-id', 'category_id'],
        ['data-task-id', 'task_id'],
    ];

    function compactText(value, maxLength) {
        return String(value || '').replace(/\s+/g, ' ').trim().slice(0, maxLength);
    }

    function safePageUrl() {
        const current = new URL(window.location.href);
        const clean = new URL(current.pathname, current.origin);
        for (const [key, value] of current.searchParams.entries()) {
            if (!SAFE_QUERY_KEYS.has(key)) continue;
            if (!/^[\w.:,-]{1,80}$/u.test(value)) continue;
            clean.searchParams.append(key, value);
        }
        return clean.toString();
    }

    function collectEntities() {
        const entities = {};
        let total = 0;
        for (const [attribute, key] of ENTITY_ATTRIBUTES) {
            const values = new Set();
            document.querySelectorAll(`[${attribute}]`).forEach((element) => {
                if (total >= 24 || values.size >= 12) return;
                const value = compactText(element.getAttribute(attribute), 64);
                if (!/^[A-Za-z0-9_-]{1,64}$/.test(value)) return;
                if (
                    ['product_id', 'listing_id', 'account_id'].includes(key)
                    && (!/^\d+$/.test(value) || Number(value) <= 0)
                ) return;
                if (!values.has(value)) total += 1;
                values.add(value);
            });
            if (values.size) entities[key] = Array.from(values);
            if (total >= 24) break;
        }
        return entities;
    }

    function contextHash(context) {
        const url = new URL((context && context.url) || window.location.href);
        const entities = Object.entries((context && context.entities) || {})
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([key, values]) => `${key}:${[...values].sort().join(',')}`)
            .join('|');
        const source = `${(context && context.route) || ''}|${url.pathname}|${entities}`;
        let hash = 2166136261;
        for (let index = 0; index < source.length; index += 1) {
            hash ^= source.charCodeAt(index);
            hash = Math.imul(hash, 16777619);
        }
        return (hash >>> 0).toString(36);
    }

    window.aiChatPopup = function () {
        return {
            open: false,
            draft: '',
            sending: false,
            loading: false,
            error: '',
            messages: [],
            conversationId: '',
            contextAttached: true,
            pageContext: null,
            contextFingerprint: '',
            pollTimer: null,
            creating: null,
            _visibilityHandler: null,

            init() {
                this.refreshPageContext();
                try { sessionStorage.removeItem(STORAGE_KEY); } catch (error) {}
                this._visibilityHandler = () => {
                    if (!document.hidden && this.open && this.conversationId) this.loadConversation(false);
                };
                document.addEventListener('visibilitychange', this._visibilityHandler);
            },

            destroy() {
                clearTimeout(this.pollTimer);
                if (this._visibilityHandler) document.removeEventListener('visibilitychange', this._visibilityHandler);
            },

            get fullChatHref() {
                const base = this.$el.dataset.chatUrl || '/agents';
                return this.conversationId ? `${base}?conversation=${encodeURIComponent(this.conversationId)}` : base;
            },

            get contextLabel() {
                const entities = this.pageContext && this.pageContext.entities ? this.pageContext.entities : {};
                const products = entities.product_id || [];
                if (products.length === 1) return `Товар #${products[0]} · ${this.pageName()}`;
                const listings = entities.listing_id || [];
                const codes = entities.marketplace_code || [];
                if (listings.length === 1 && codes.length === 1) {
                    return `${codes[0].toUpperCase()} #${listings[0]} · ${this.pageName()}`;
                }
                const count = Object.values(entities).reduce((sum, values) => sum + values.length, 0);
                if (count) return `${this.pageName()} · ${count} объектов`;
                return this.pageName();
            },

            pageName() {
                const title = compactText(document.title, 160).replace(/\s+[—|-]\s+Seller Hub$/i, '');
                return title || 'Текущая страница';
            },

            refreshPageContext() {
                const nextContext = {
                    title: compactText(document.title, 200),
                    url: safePageUrl(),
                    route: compactText(this.$el.dataset.route, 120),
                    entities: collectEntities(),
                };
                const nextFingerprint = contextHash(nextContext);
                if (this.contextFingerprint && this.contextFingerprint !== nextFingerprint) {
                    clearTimeout(this.pollTimer);
                    this.conversationId = '';
                    this.messages = [];
                }
                this.pageContext = nextContext;
                this.contextFingerprint = nextFingerprint;
                if (!this.conversationId) {
                    try {
                        this.conversationId = sessionStorage.getItem(this.contextStorageKey()) || '';
                    } catch (error) {}
                }
                this.contextAttached = true;
            },

            contextStorageKey() {
                return `${STORAGE_KEY}:${this.contextFingerprint || 'page'}`;
            },

            togglePanel() {
                if (this.open) this.closePanel();
                else this.openPanel();
            },

            openPanel() {
                this.refreshPageContext();
                this.open = true;
                this.error = '';
                if (this.conversationId) this.loadConversation(false);
                this.$nextTick(() => {
                    this.$refs.composer && this.$refs.composer.focus();
                    this.scrollToBottom(false);
                });
            },

            closePanel() {
                if (!this.open) return;
                this.open = false;
                clearTimeout(this.pollTimer);
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
                field.style.height = `${Math.min(field.scrollHeight, 112)}px`;
            },

            handleEnter(event) {
                if (event.isComposing || event.shiftKey) return;
                event.preventDefault();
                this.sendMessage();
            },

            async ensureConversation() {
                if (this.conversationId) return this.conversationId;
                if (this.creating) return this.creating;
                this.creating = this.api('/agents/api/conversations', {
                    method: 'POST',
                    body: JSON.stringify({ title: `Со страницы: ${this.pageName()}` }),
                }).then((payload) => {
                    this.conversationId = payload.conversation.id;
                    try { sessionStorage.setItem(this.contextStorageKey(), this.conversationId); } catch (error) {}
                    return this.conversationId;
                }).finally(() => { this.creating = null; });
                return this.creating;
            },

            scopedProductIds() {
                if (!this.contextAttached || !this.pageContext) return [];
                const values = this.pageContext.entities.product_id || [];
                if (values.length !== 1 || !/^\d+$/.test(values[0])) return [];
                const id = Number(values[0]);
                return Number.isSafeInteger(id) && id > 0 ? [id] : [];
            },

            marketplaceEntityScope() {
                if (!this.contextAttached || !this.pageContext) return null;
                const entities = this.pageContext.entities || {};
                const listingValues = entities.listing_id || [];
                const accountValues = entities.account_id || [];
                const codeValues = entities.marketplace_code || [];
                if (
                    listingValues.length !== 1
                    || accountValues.length !== 1
                    || codeValues.length !== 1
                ) return null;
                const listingId = Number(listingValues[0]);
                const accountId = Number(accountValues[0]);
                const code = String(codeValues[0]).trim().toLowerCase();
                if (
                    !Number.isSafeInteger(listingId) || listingId <= 0
                    || !Number.isSafeInteger(accountId) || accountId <= 0
                    || !/^[a-z][a-z0-9_-]{1,49}$/.test(code)
                ) return null;
                return {
                    kind: 'marketplace_listing',
                    ids: [listingId],
                    marketplace_code: code,
                    account_id: accountId,
                    scope_mode: 'selected',
                };
            },

            async sendMessage() {
                const text = this.draft.trim();
                if (!text || this.sending) return;
                this.sending = true;
                this.error = '';
                this.draft = '';
                const pendingId = `pending-${Date.now()}`;
                this.messages.push({
                    id: pendingId, role: 'user', kind: 'text', content: text,
                    metadata: {}, created_at: new Date().toISOString(),
                });
                this.$nextTick(() => {
                    this.resizeComposer();
                    this.scrollToBottom(true);
                });

                const send = async (allowRetry) => {
                    const conversationId = await this.ensureConversation();
                    const marketplaceScope = this.marketplaceEntityScope();
                    const productIds = marketplaceScope ? [] : this.scopedProductIds();
                    try {
                        return await this.api(`/agents/api/conversations/${conversationId}/messages`, {
                            method: 'POST',
                            body: JSON.stringify({
                                message: text,
                                product_ids: productIds,
                                entity_kind: marketplaceScope ? 'marketplace_listing' : null,
                                entity_scope: marketplaceScope,
                                page_context: this.contextAttached ? this.pageContext : null,
                                scope_mode: marketplaceScope
                                    ? null
                                    : (productIds.length ? 'page' : 'global'),
                            }),
                        });
                    } catch (error) {
                        if (allowRetry && error.status === 404) {
                            this.resetConversation();
                            return send(false);
                        }
                        throw error;
                    }
                };

                try {
                    const payload = await send(true);
                    this.messages = this.messages.filter((message) => message.id !== pendingId);
                    this.mergeMessages(payload.messages || []);
                    this.schedulePoll(this.hasActiveRun() ? 1400 : 0);
                } catch (error) {
                    this.messages = this.messages.filter((message) => message.id !== pendingId);
                    this.draft = text;
                    this.error = error.message || 'Не удалось отправить сообщение';
                    this.$nextTick(() => this.resizeComposer());
                } finally {
                    this.sending = false;
                    this.$nextTick(() => this.scrollToBottom(true));
                }
            },

            resetConversation() {
                this.conversationId = '';
                this.messages = [];
                try { sessionStorage.removeItem(this.contextStorageKey()); } catch (error) {}
            },

            async loadConversation(showError) {
                if (!this.conversationId || this.loading || document.hidden) return;
                this.loading = true;
                try {
                    const payload = await this.api(`/agents/api/conversations/${this.conversationId}`);
                    this.mergeMessages(payload.messages || [], true);
                    this.schedulePoll(this.payloadHasActiveRun(payload) ? 1400 : 0);
                    this.$nextTick(() => this.scrollToBottom(false));
                } catch (error) {
                    if (error.status === 404) this.resetConversation();
                    else if (showError) this.error = error.message;
                } finally {
                    this.loading = false;
                }
            },

            mergeMessages(incoming, replace) {
                const normalized = incoming.map((message) => ({
                    ...message,
                    metadata: message.metadata || {},
                }));
                if (replace) {
                    this.messages = normalized.slice(-24);
                    return;
                }
                const byId = new Map(this.messages.map((message) => [message.id, message]));
                normalized.forEach((message) => byId.set(message.id, message));
                this.messages = Array.from(byId.values())
                    .sort((left, right) => new Date(left.created_at) - new Date(right.created_at))
                    .slice(-24);
            },

            payloadHasActiveRun(payload) {
                if (payload.run && ['queued', 'running'].includes(payload.run.status)) return true;
                return (payload.messages || []).some((message) =>
                    message.kind === 'run' && ['queued', 'running'].includes((message.metadata || {}).status)
                );
            },

            hasActiveRun() {
                return this.messages.some((message) =>
                    message.kind === 'run' && ['queued', 'running'].includes((message.metadata || {}).status)
                );
            },

            schedulePoll(delay) {
                clearTimeout(this.pollTimer);
                if (!delay || !this.open || document.hidden) return;
                this.pollTimer = setTimeout(() => this.loadConversation(false), delay);
            },

            displayContent(message) {
                const content = String(message.content || '');
                if (message.kind === 'run') {
                    const status = (message.metadata || {}).status;
                    if (status === 'queued') return message.content || 'Задача поставлена в очередь.';
                    if (status === 'running') return message.content || 'Выполняю задачу.';
                }
                return content;
            },

            imageArtifacts(message) {
                const result = (((message || {}).metadata || {}).result || {});
                const artifacts = [];
                for (const step of result.results || []) {
                    const stepArtifacts = (((step || {}).result || {}).artifacts || []);
                    for (const artifact of stepArtifacts) {
                        if (!artifact || artifact.type !== 'image_generation') continue;
                        artifacts.push(artifact);
                    }
                }
                return artifacts.slice(0, 2);
            },

            popupImageStatus(artifact) {
                if ((artifact || {}).has_final && (artifact || {}).status === 'completed') {
                    const cost = Number((artifact || {}).estimated_cost_rub);
                    const label = Number.isFinite(cost)
                        ? new Intl.NumberFormat('ru-RU', {
                            minimumFractionDigits: 2, maximumFractionDigits: 2,
                        }).format(cost) + ' ₽'
                        : '';
                    return `Готово${label ? ` · ${label}` : ''} · нужна проверка`;
                }
                return ({
                    queued: 'В очереди', running: 'Создаётся',
                    remote_running: 'Создаётся', finalizing: 'Собирается',
                    failed: 'Ошибка', cancelled: 'Остановлено',
                })[(artifact || {}).status] || 'Открыть результат';
            },

            scrollToBottom(smooth) {
                const node = this.$refs.messages;
                if (!node) return;
                node.scrollTo({ top: node.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
            },

            async api(url, options) {
                const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
                const response = await fetch(url, {
                    ...(options || {}),
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrf,
                        ...((options && options.headers) || {}),
                    },
                });
                let payload = {};
                try { payload = await response.json(); } catch (error) {}
                if (!response.ok) {
                    const requestError = new Error(payload.error || 'Ошибка запроса');
                    requestError.status = response.status;
                    throw requestError;
                }
                return payload;
            },
        };
    };
})();
