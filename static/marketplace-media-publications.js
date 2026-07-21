(function () {
  'use strict';

  const labels = {
    draft: 'Только предпросмотр', ready: 'Готово к подтверждению',
    queued: 'В очереди', preflighting: 'Повторная проверка',
    submitting: 'Отправка', reconciling: 'Сверка с каналом',
    running: 'В работе', succeeded: 'Подтверждено', partial: 'Есть проблемы',
    uncertain: 'Требует сверки', failed: 'Не применено', conflict: 'Конфликт',
    blocked: 'Недоступно', cancelled: 'Отменено'
  };

  function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || '';
  }

  function toast(message, type) {
    const store = window.Alpine && window.Alpine.store('toasts');
    const method = type === 'error' ? 'error' : type === 'warning' ? 'warning' : 'success';
    if (store && typeof store[method] === 'function') store[method](message);
    else if (type === 'error') window.console.error(message);
  }

  async function post(path, payload) {
    const response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken()},
      body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.error || 'Не удалось выполнить действие');
    return data;
  }

  window.mediaPublicationWizard = function () {
    const boot = window.MEDIA_PUBLICATION_WIZARD || {};
    const approved = Array.isArray(boot.approvedItemIds) ? boot.approvedItemIds : [];
    const params = new URLSearchParams(window.location.search);
    const requested = params.getAll('item_ids').map((value) => Number(value)).filter((value) => Number.isInteger(value) && value > 0);
    const allowed = new Set(approved);
    const selected = [...new Set(requested.filter((id) => allowed.has(id)))];
    return {
      campaignId: boot.campaignId,
      approvedItemIds: approved,
      selection: selected.length ? selected : approved.slice(),
      requestedCount: selected.length,
      busy: false,

      useAll() { this.selection = this.approvedItemIds.slice(); },

      async prepare(marketplaceCode, accountId) {
        if (!this.selection.length || this.busy) return;
        this.busy = true;
        try {
          const data = await post(`/image-lab/api/campaigns/${this.campaignId}/publications`, {
            marketplace_code: marketplaceCode,
            account_id: accountId,
            item_ids: this.selection
          });
          window.location.assign(data.redirect_url);
        } catch (error) {
          toast(error.message, 'error');
        } finally {
          this.busy = false;
        }
      }
    };
  };

  window.mediaPublicationPage = function () {
    const boot = window.MEDIA_PUBLICATION_BOOTSTRAP || {};
    return {
      publication: boot.publication || {operations: []},
      publicHost: boot.publicHost || {ready: false, message: ''},
      confirmExact: false,
      rollbackSelection: [],
      busy: false,
      pollTimer: null,

      init() { this.schedulePoll(); },
      statusLabel(status) { return labels[status] || status; },
      targetLabel(operation) {
        if (operation.marketplace_code === 'wb') return `WB nmID ${operation.external_item_id} · операция #${operation.id}`;
        return `Ozon listing #${operation.listing_id || '—'} · кабинет #${operation.account_id || '—'}`;
      },
      get canConfirm() {
        return !this.publication.confirmed_at && this.publication.ready_items > 0;
      },
      get canCancel() {
        return ['ready', 'queued', 'running'].includes(this.publication.status) && this.publication.succeeded_items === 0;
      },
      get rollbackableIds() {
        return (this.publication.operations || []).filter((operation) => operation.can_rollback).map((operation) => operation.id);
      },
      get allRollbackSelected() {
        const ids = this.rollbackableIds;
        return ids.length > 0 && ids.every((id) => this.rollbackSelection.includes(id));
      },
      toggleRollback(checked) {
        this.rollbackSelection = checked ? this.rollbackableIds.slice() : [];
      },

      async refresh() {
        try {
          const response = await fetch(`/image-lab/api/publications/${this.publication.id}`, {headers: {'Accept': 'application/json'}});
          const data = await response.json();
          if (!response.ok || !data.success) throw new Error(data.error || 'Не удалось обновить статусы');
          this.publication = data.publication;
          this.publicHost = data.public_host;
          const available = new Set(this.rollbackableIds);
          this.rollbackSelection = this.rollbackSelection.filter((id) => available.has(id));
        } catch (error) {
          toast(error.message, 'error');
        }
      },
      schedulePoll() {
        if (this.pollTimer) window.clearTimeout(this.pollTimer);
        const active = ['queued', 'running'].includes(this.publication.status)
          || (this.publication.operations || []).some((operation) => ['queued', 'preflighting', 'submitting', 'reconciling', 'uncertain'].includes(operation.status))
          || (this.publication.rollback_operations || []).some((operation) => ['queued', 'preflighting', 'submitting', 'reconciling', 'uncertain'].includes(operation.status));
        if (!active) return;
        this.pollTimer = window.setTimeout(async () => {
          await this.refresh();
          this.schedulePoll();
        }, 3000);
      },

      async confirmPublication() {
        if (!this.confirmExact || this.busy) return;
        this.busy = true;
        try {
          const data = await post(`/image-lab/api/publications/${this.publication.id}/confirm`, {
            expected_version: this.publication.version,
            confirm_exact_order: true
          });
          this.publication = data.publication;
          this.confirmExact = false;
          toast(`Поставлено в очередь: ${data.queued}`);
          this.schedulePoll();
        } catch (error) { toast(error.message, 'error'); }
        finally { this.busy = false; }
      },

      async cancelPublication() {
        if (!window.confirm('Отменить все операции, которые ещё не пересекли provider write boundary?')) return;
        this.busy = true;
        try {
          const data = await post(`/image-lab/api/publications/${this.publication.id}/cancel`, {});
          this.publication = data.publication;
          toast(`Отменено до write: ${data.cancelled}`);
        } catch (error) { toast(error.message, 'error'); }
        finally { this.busy = false; }
      },

      async rollbackSelected() {
        if (!this.rollbackSelection.length || this.busy) return;
        if (!window.confirm(`Восстановить исходную галерею для ${this.rollbackSelection.length} карточек? Перед write будет повторная live-сверка.`)) return;
        this.busy = true;
        try {
          const data = await post(`/image-lab/api/publications/${this.publication.id}/rollbacks`, {
            operation_ids: this.rollbackSelection,
            confirm_exact_state: true
          });
          this.publication = data.publication;
          this.rollbackSelection = [];
          toast(`Откатов поставлено в очередь: ${data.queued}`);
          this.schedulePoll();
        } catch (error) { toast(error.message, 'error'); }
        finally { this.busy = false; }
      }
    };
  };
})();
