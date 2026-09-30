/* Canonical source chooser: explicit selection, bounded reads, no publication. */
function draftSourcePicker(endpoint, initial) {
    return {
        query: '', selectedId: '', selectedLabel: '', open: false,
        items: initial.items || [], hasMore: !!initial.has_more,
        loading: false, error: '', active: -1, timer: null, controller: null, revision: 0,
        label(item) { return '#' + item.id + ' · ' + item.title; },
        changed() {
            this.selectedId = '';
            this.selectedLabel = '';
            this.active = -1;
            this.open = true;
            this.error = '';
            this.items = [];
            this.loading = true;
            this.revision += 1;
            this.controller?.abort();
            clearTimeout(this.timer);
            this.$refs.sourceQuery.setCustomValidity('Выберите товар из результатов поиска.');
            const revision = this.revision;
            this.timer = setTimeout(() => this.search(revision), 250);
        },
        async search(revision = this.revision) {
            this.loading = true;
            this.error = '';
            this.controller = new AbortController();
            const controller = this.controller;
            let timedOut = false;
            const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
            try {
                const response = await fetch(endpoint + '?q=' + encodeURIComponent(this.query.trim()), {
                    headers: {'Accept': 'application/json'}, signal: controller.signal,
                });
                if (response.status === 401 || response.redirected) throw new Error('Сессия истекла. Войдите снова.');
                if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('Не удалось получить результаты поиска. Повторите попытку.');
                const data = await response.json();
                if (!response.ok) throw new Error(response.status === 401 ? 'Сессия истекла. Войдите снова.' : data.error || 'Не удалось найти товары. Повторите поиск.');
                if (revision !== this.revision) return;
                this.items = data.items || [];
                this.hasMore = !!data.has_more;
                this.active = -1;
            } catch (error) {
                if (revision === this.revision) {
                    if (timedOut) this.error = 'Поиск занял слишком много времени. Повторите попытку.';
                    else if (error.name !== 'AbortError') this.error = error instanceof TypeError ? 'Не удалось связаться с сервером.' : error.message || 'Не удалось связаться с сервером.';
                }
            } finally {
                clearTimeout(timeout);
                if (revision === this.revision) this.loading = false;
            }
        },
        choose(item) {
            this.revision += 1;
            clearTimeout(this.timer);
            this.controller?.abort();
            this.loading = false;
            this.selectedId = String(item.id);
            this.selectedLabel = this.label(item);
            // Keep search queries <=100 even if a source title is longer.
            this.query = item.title.slice(0, 100);
            this.active = -1;
            this.$refs.sourceQuery.setCustomValidity('');
            this.$refs.sourceQuery.focus();
            this.open = false;
        },
        move(direction) {
            this.open = true;
            if (!this.items.length) return;
            this.active = this.active < 0 ? (direction > 0 ? 0 : this.items.length - 1)
                : (this.active + direction + this.items.length) % this.items.length;
            this.$nextTick(() => document.getElementById('draft-source-option-' + this.active)?.scrollIntoView({block: 'nearest'}));
        },
        enter(event) {
            if (!this.open) return;
            event.preventDefault();
            if (this.active >= 0 && this.items[this.active]) this.choose(this.items[this.active]);
        },
        submit(event) {
            if (!this.selectedId) {
                event.preventDefault();
                this.$refs.sourceQuery.setCustomValidity('Найдите и выберите товар из списка.');
                this.$refs.sourceQuery.reportValidity();
                this.open = true;
            }
        },
        destroy() { clearTimeout(this.timer); this.controller?.abort(); },
    };
}
