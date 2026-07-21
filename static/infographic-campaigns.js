(function () {
  'use strict';

  const labels = {
    queued: 'В очереди', running: 'Генерация', review: 'Нужна проверка',
    approved: 'Одобрено', partial: 'Есть замечания', cancelled: 'Отменено',
    ready: 'Готово', failed: 'Ошибка', blocked: 'Недостаточно данных',
    conflict: 'Карточка изменилась'
  };

  window.infographicCampaignPage = function () {
    const boot = window.INFOGRAPHIC_CAMPAIGN_BOOTSTRAP || {};
    return {
      campaign: boot.campaign || {},
      items: boot.items || [],
      page: boot.page || 1,
      pages: boot.pages || 1,
      selectedItems: [],
      search: '',
      statusFilter: 'all',
      busy: false,
      pollTimer: null,

      init() {
        this.schedulePoll();
      },

      get filteredItems() {
        const needle = this.search.trim().toLocaleLowerCase('ru');
        return this.items.filter((item) => {
          if (needle && !String(item.title || '').toLocaleLowerCase('ru').includes(needle)) return false;
          if (this.statusFilter === 'active') return ['queued', 'running'].includes(item.status);
          if (this.statusFilter === 'ready') return item.status === 'ready' && item.approved_slides < item.completed_slides;
          if (this.statusFilter === 'problem') return ['failed', 'blocked', 'conflict'].includes(item.status);
          if (this.statusFilter === 'approved') return item.completed_slides > 0 && item.approved_slides === item.completed_slides;
          return true;
        });
      },

      get allVisibleSelected() {
        const ids = this.filteredItems.map((item) => item.id);
        return ids.length > 0 && ids.every((id) => this.selectedItems.includes(id));
      },

      get selectedReady() {
        const selected = new Set(this.selectedItems);
        return this.items.filter((item) => selected.has(item.id) && item.status === 'ready' && item.completed_slides > 0).map((item) => item.id);
      },

      get selectedRetryable() {
        const selected = new Set(this.selectedItems);
        return this.items.filter((item) => selected.has(item.id) && ['failed', 'blocked', 'conflict'].includes(item.status)).map((item) => item.id);
      },

      toggleVisible(checked) {
        const visible = new Set(this.filteredItems.map((item) => item.id));
        if (checked) this.selectedItems = [...new Set([...this.selectedItems, ...visible])];
        else this.selectedItems = this.selectedItems.filter((id) => !visible.has(id));
      },

      statusLabel(status) { return labels[status] || status; },
      reviewLabel(status) {
        return {pending: 'Ждёт решения', approved: 'Одобрен', rejected: 'Отклонён'}[status] || status;
      },

      notify(message, type) {
        const store = window.Alpine && window.Alpine.store('toasts');
        const method = type === 'error' ? 'error' : type === 'warning' ? 'warning' : 'success';
        if (store && typeof store[method] === 'function') store[method](message);
        else if (type === 'error') window.console.error(message);
      },

      async request(path, payload) {
        this.busy = true;
        try {
          const response = await fetch(path, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || ''
            },
            body: JSON.stringify(payload || {})
          });
          const data = await response.json();
          if (!response.ok || !data.success) throw new Error(data.error || 'Не удалось выполнить действие');
          if (data.campaign) this.campaign = data.campaign;
          return data;
        } finally {
          this.busy = false;
        }
      },

      async refresh() {
        try {
          const response = await fetch(`/image-lab/api/campaigns/${this.campaign.id}?page=${this.page}`, {headers: {'Accept': 'application/json'}});
          const data = await response.json();
          if (!response.ok || !data.success) throw new Error(data.error || 'Не удалось обновить кампанию');
          this.campaign = data.campaign;
          this.items = data.items || [];
          this.pages = data.pages || 1;
          const available = new Set(this.items.map((item) => item.id));
          this.selectedItems = this.selectedItems.filter((id) => available.has(id));
        } catch (error) {
          this.notify(error.message, 'error');
        }
      },

      schedulePoll() {
        if (this.pollTimer) window.clearTimeout(this.pollTimer);
        if (!['queued', 'running'].includes(this.campaign.status)) return;
        this.pollTimer = window.setTimeout(async () => {
          await this.refresh();
          this.schedulePoll();
        }, 2500);
      },

      async reviewItems(action) {
        const ids = this.selectedReady;
        if (!ids.length) return;
        try {
          const data = await this.request(`/image-lab/api/campaigns/${this.campaign.id}/review`, {action, item_ids: ids});
          await this.refresh();
          this.selectedItems = [];
          this.notify(`${data.reviewed} слайдов: ${action === 'approve' ? 'одобрено' : 'отклонено'}`);
        } catch (error) { this.notify(error.message, 'error'); }
      },

      async reviewSlide(slideId, action) {
        try {
          await this.request(`/image-lab/api/campaigns/${this.campaign.id}/review`, {action, slide_ids: [slideId]});
          await this.refresh();
          this.notify(action === 'approve' ? 'Слайд одобрен' : 'Слайд отклонён');
        } catch (error) { this.notify(error.message, 'error'); }
      },

      async retryItems() {
        const ids = this.selectedRetryable;
        if (!ids.length) return;
        try {
          const data = await this.request(`/image-lab/api/campaigns/${this.campaign.id}/retry`, {item_ids: ids});
          await this.refresh();
          this.selectedItems = [];
          this.notify(`Возвращено в очередь: ${data.queued_items}`);
          this.schedulePoll();
        } catch (error) { this.notify(error.message, 'error'); }
      },

      async retryOne(itemId) {
        this.selectedItems = [itemId];
        await this.retryItems();
      },

      async cancelCampaign() {
        if (!window.confirm('Остановить ещё не начатые товары? Уже собранные слайды сохранятся.')) return;
        try {
          const data = await this.request(`/image-lab/api/campaigns/${this.campaign.id}/cancel`, {});
          await this.refresh();
          this.notify(`Очередь остановлена. Отменено товаров: ${data.cancelled_items}`);
        } catch (error) { this.notify(error.message, 'error'); }
      },

      openPublicationWizard() {
        const selected = new Set(this.selectedItems);
        const approved = this.items.filter((item) => item.approved_slides > 0);
        const exact = approved.filter((item) => selected.has(item.id)).map((item) => item.id);
        const params = new URLSearchParams();
        exact.forEach((id) => params.append('item_ids', String(id)));
        const suffix = params.toString() ? `?${params.toString()}` : '';
        window.location.assign(`/image-lab/campaigns/${this.campaign.id}/publication/new${suffix}`);
      }
    };
  };
})();
