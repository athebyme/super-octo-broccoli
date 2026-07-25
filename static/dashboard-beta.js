/* Главная · beta — отвечает на вопрос «чем заняться сегодня».
   Деньги сверху, задачи с прямым действием ниже, ничего лишнего. */
(function () {
    'use strict';

    if (typeof window.shVue === 'undefined') return;
    var boot = window.shVue.bootstrapData('db-bootstrap');

    var PERIODS = [
        { key: '7d', label: 'Неделя', hint: 'за 7 дней' },
        { key: '30d', label: 'Месяц', hint: 'за 30 дней' },
        { key: '90d', label: 'Квартал', hint: 'за 90 дней' },
    ];

    /* Почему сигнал важен — на языке последствий, а не метрик */
    var WHY = {
        losing_sales: 'Покупатель уже нашёл товар — значит, деньги на рекламу и выдачу отработали. Уходит он из-за фото, описания или цены, и каждый такой уход это упущенная продажа.',
        no_photos: 'Карточка без фотографий не проходит модерацию и не участвует в выдаче. Пока фото нет, товар просто не существует для покупателя.',
        supplier_updates: 'Поставщик уточнил данные: описание, характеристики или фото. Пока вы их не подтянули, на витрине висит устаревшая информация.',
        ready_to_publish: 'Эти карточки уже проверены и готовы. Пока они не отправлены, товар не продаётся.',
        listing_errors: 'Площадка отклонила карточку. До исправления ошибки товар не показывается покупателям.',
        prices_waiting: 'Мы отправили цены и ждём, пока площадка их применит. Отметку «применено» ставим только после того, как увидим новую цену на витрине.',
        prices_partial: 'Часть позиций площадка не приняла. Их можно отправить повторно — цены пересчитаются от текущих значений.',
    };

    window.shVue.mount('#dashboard-app', {
        data: function () {
            return {
                hasAnyChannel: !!boot.hasAnyChannel,
                wbConnected: !!boot.wbConnected,
                userName: boot.userName || '',
                urls: boot.urls || {},

                period: '30d',
                periods: PERIODS,
                signals: [],
                tasks: [],
                money: {
                    revenue: '—', orders: '—', average: '—',
                    revenueTrend: null, ordersTrend: null,
                },
                quality: { avg: '—', hint: '' },
                loading: { money: true, attention: true, quality: true },
                drawer: { open: false, signal: null },
            };
        },

        computed: {
            greeting: function () {
                var hour = new Date().getHours();
                var part = hour < 5 ? 'Доброй ночи'
                    : hour < 12 ? 'Доброе утро'
                    : hour < 18 ? 'Добрый день' : 'Добрый вечер';
                return this.userName ? part + ', ' + this.userName : part;
            },
            subtitle: function () {
                if (!this.hasAnyChannel) return 'Подключите магазин, чтобы начать';
                if (this.loading.attention) return 'Смотрим, что происходит с вашими товарами…';
                if (!this.signals.length) return 'Срочных задач нет — можно заняться новыми товарами';
                var total = this.signals.reduce(function (sum, s) { return sum + s.count; }, 0);
                return 'Внимания требуют ' + this.$fmt.int(total) + ' '
                    + this.$fmt.plural(total, 'карточка', 'карточки', 'карточек');
            },
            periodHint: function () {
                var found = PERIODS.filter(function (p) { return p.key === this.period; }.bind(this));
                return found.length ? found[0].hint : '';
            },
        },

        mounted: function () {
            if (!this.hasAnyChannel) {
                this.loading = { money: false, attention: false, quality: false };
                return;
            }
            this.loadAttention();
            this.loadMoney();
            this.loadQuality();
            this.loadTasks();
        },

        methods: {
            setPeriod: function (key) {
                if (this.period === key) return;
                this.period = key;
                this.loadMoney();
            },
            loadAttention: function () {
                var self = this;
                this.loading.attention = true;
                this.$api.get(this.urls.attention).then(function (data) {
                    self.signals = data.signals || [];
                }).catch(function () {
                    self.signals = [];
                }).finally(function () {
                    self.loading.attention = false;
                });
            },
            loadMoney: function () {
                var self = this;
                if (!this.wbConnected) {
                    this.loading.money = false;
                    return;
                }
                this.loading.money = true;
                this.$api.get(this.urls.analytics, { period: this.period })
                    .then(function (payload) {
                        var kpi = ((payload.data || {}).kpi) || {};
                        var revenue = kpi.revenue || 0;
                        var orders = kpi.orders || 0;
                        self.money.revenue = self.$fmt.money(revenue);
                        self.money.orders = self.$fmt.int(orders);
                        self.money.average = orders
                            ? self.$fmt.money(revenue / orders)
                            : '—';
                        var dynamics = (payload.data || {}).dynamics || {};
                        self.money.revenueTrend = typeof dynamics.revenue_change === 'number'
                            ? dynamics.revenue_change : null;
                        self.money.ordersTrend = typeof dynamics.orders_change === 'number'
                            ? dynamics.orders_change : null;
                    }).catch(function () {
                        // Нет данных — показываем прочерк, а не выдуманный ноль
                        self.money.revenue = '—';
                        self.money.orders = '—';
                        self.money.average = '—';
                    }).finally(function () {
                        self.loading.money = false;
                    });
            },
            loadQuality: function () {
                var self = this;
                this.$api.get(this.urls.quality).then(function (payload) {
                    var data = payload.data || {};
                    self.quality.avg = data.avg_quality === null || data.avg_quality === undefined
                        ? '—' : String(data.avg_quality);
                    self.quality.hint = data.need_attention
                        ? self.$fmt.int(data.need_attention) + ' требуют внимания'
                        : 'все карточки в порядке';
                }).catch(function () {
                    self.quality.avg = '—';
                }).finally(function () {
                    self.loading.quality = false;
                });
            },
            loadTasks: function () {
                var self = this;
                this.$api.get(this.urls.tasks).then(function (payload) {
                    var items = payload.items || payload.tasks || [];
                    self.tasks = items.filter(function (task) {
                        return task && task.status !== 'done';
                    }).slice(0, 5);
                }).catch(function () {
                    self.tasks = [];
                });
            },
            openSignal: function (signal) {
                this.drawer.signal = signal;
                this.drawer.open = true;
            },
            whyText: function (key) {
                return WHY[key] || 'Эта задача влияет на то, увидит ли покупатель ваш товар.';
            },
        },
    });
})();
