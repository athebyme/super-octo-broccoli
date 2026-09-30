/* Local observation review. Approval queues an official re-read in the worker. */
(() => {
  'use strict';
  const host = document.getElementById('ozon-reference-review');
  if (!host || !window.Vue) return;
  Vue.createApp({
    data: () => ({review: null, loading: false, confirming: false, confirmed: false,
      polls: 0, error: '', mode: 'removed', page: 1, controller: null, timer: null, stopped: false}),
    computed: {
      pages() { return Math.max(1, Math.ceil((this.review?.total || 0) / 100)); },
      title() { return {pending_review:'Ozon изменил словарь', approved:'Повторно сверяем с Ozon',
        applied:'Новый словарь применён', stale:'Предыдущее сравнение устарело'}[this.review?.status] || ''; },
    },
    methods: {
      date(value) { return new Date(value).toLocaleString('ru-RU', {dateStyle:'short', timeStyle:'short'}); },
      schedule() {
        clearTimeout(this.timer);
        if (!this.stopped && !document.hidden && this.polls < 60 && this.review?.status === 'approved')
          this.timer = setTimeout(() => { this.polls += 1; this.load(); }, 10000);
      },
      async request(url, options = {}) {
        this.controller = new AbortController();
        const timer = setTimeout(() => this.controller?.abort(), 10000);
        try {
          const response = await fetch(url, {...options, signal:this.controller.signal,
            credentials:'same-origin', redirect:'follow'});
          if (response.redirected || response.status === 401 || response.status === 403) {
            this.stopped = true;
            throw new Error('Сессия завершена или доступ изменился. Войдите в аккаунт и откройте страницу снова.');
          }
          const data = await response.json().catch(() => { throw new Error('Сервер вернул неполный ответ. Обновите данные.'); });
          if (!response.ok || !data.success) throw new Error(data.error || 'Не удалось получить ответ. Обновите данные.');
          return data;
        } catch (error) {
          if (error instanceof TypeError) throw new Error('Нет ответа сервера. Обновите данные, чтобы проверить состояние.');
          throw error;
        } finally { clearTimeout(timer); this.controller = null; }
      },
      async load() {
        if (this.loading || this.confirming || this.stopped || document.hidden) return;
        this.loading = true; this.error = '';
        try {
          const data = await this.request(`${host.dataset.url}?mode=${this.mode}&page=${this.page}`);
          if (this.review?.version !== data.review?.version) this.confirmed = false;
          this.review = data.review;
        } catch (error) {
          this.confirmed = false;
          if (this.review) { this.mode = this.review.mode; this.page = this.review.page; this.review.can_approve = false; }
          this.error = error.name === 'AbortError' ? 'Сервер отвечает дольше обычного. Попробуйте обновить данные.' : error.message;
        } finally { this.loading = false; this.schedule(); }
      },
      async approve() {
        if (!this.confirmed || !this.review?.can_approve || this.loading || this.confirming) return;
        this.confirming = true; this.error = ''; clearTimeout(this.timer);
        try {
          await this.request(host.dataset.url, {
            method:'POST', headers:{'Content-Type':'application/json',
              'X-CSRFToken':document.querySelector('meta[name="csrf-token"]')?.content || ''},
            body:JSON.stringify({version:this.review.version, candidate_hash:this.review.candidate_hash, confirm:true}),
          });
          this.review.status = 'approved'; this.review.can_approve = false; this.confirmed = false;
        } catch (error) {
          this.confirmed = false; this.review.can_approve = false;
          this.error = error.name === 'AbortError' ? 'Ответ на подтверждение не получен. Обновите данные, чтобы проверить его состояние.' : error.message;
        } finally { this.confirming = false; this.schedule(); }
      },
      setMode(value) { if (this.loading || this.confirming) return; this.mode = value; this.page = 1; this.load(); },
      move(delta) { this.page += delta; this.load(); },
      visibility() {
        if (document.hidden) clearTimeout(this.timer);
        else this.load();
      },
    },
    mounted() { this.load(); document.addEventListener('visibilitychange', this.visibility); },
    beforeUnmount() { this.stopped = true; clearTimeout(this.timer); this.controller?.abort();
      document.removeEventListener('visibilitychange', this.visibility); },
  }).mount(host);
})();
