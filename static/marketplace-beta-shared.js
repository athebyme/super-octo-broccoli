/* Общие хелперы beta-страниц каталога маркетплейсов.
   Один источник правды для цен, статусов и формата — чтобы витрина и карточка
   товара не расходились в трактовке наблюдённых фактов площадки. */
(function (global) {
    'use strict';

    var STATUS_META = {
        active: { label: 'Активен', tone: 'ok' },
        moderation: { label: 'Модерация', tone: 'info' },
        creating: { label: 'Создаётся', tone: 'info' },
        error: { label: 'Ошибка', tone: 'danger' },
        archived: { label: 'Архив', tone: 'muted' },
        inactive: { label: 'Неактивен', tone: 'muted' },
        unknown: { label: 'Без статуса', tone: 'muted' }
    };

    /* Наблюдённые provider-статусы обработки; служебные маркеры скрываются */
    var PROVIDER_STATUS_META = {
        legacy_wb_projection: null,
        price_sent: { label: 'Обработан, цена передана', tone: 'ok' },
        offer_validated: { label: 'Оффер проверен', tone: 'info' },
        pics_delivered: { label: 'Фото загружены', tone: 'info' },
        pdf_delivered: { label: 'Документы загружены', tone: 'info' },
        new: { label: 'Создаётся на площадке', tone: 'info' },
        moderating: { label: 'На модерации', tone: 'info' },
        declined: { label: 'Отклонён площадкой', tone: 'danger' },
        unmatched: { label: 'Не сопоставлен на площадке', tone: 'warn' }
    };

    var VISIBILITY_META = {
        ALL: null,
        active: { label: 'Виден на витрине', tone: 'ok' },
        inactive: {
            label: 'Не в продаже на витрине',
            tone: 'warn',
            hint: 'Площадка помечает карточку неактивной: архив, нулевой остаток или скрыта с витрины'
        },
        ARCHIVED: { label: 'Архив площадки', tone: 'muted' }
    };

    var LINK_SOURCES = {
        wb_backfill: 'из WB-каталога',
        wb_product_fk: 'из WB-карточки',
        exact_source_identity: 'точное совпадение ID поставщика',
        exact_offer_identity: 'точное совпадение артикула',
        exact_offer: 'точное совпадение артикула',
        exact_vendor: 'точное совпадение vendor code',
        manual: 'выбрана вручную',
        reconciliation: 'авто-сверка точного совпадения'
    };

    var WEIGHT_UNITS = { g: 1, kg: 1000, mg: 0.001 };
    var LENGTH_UNITS = { mm: 0.1, cm: 1, m: 100 };

    function numberOrNull(value) {
        if (typeof value !== 'number' && typeof value !== 'string') return null;
        if (typeof value === 'string' && (value.length > 40 || !/^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim()))) return null;
        var num = Number(value);
        return Number.isFinite(num) && num >= 0 && num <= 1e15 ? num : null;
    }

    function fmtInt(value) {
        return new Intl.NumberFormat('ru-RU').format(value);
    }

    /* --- Цены -----------------------------------------------------------
       Две площадки пишут price_summary_json в разной форме, и обе — факт:
       Ozon: {values: {price, old_price, marketing_seller_price, min_price}}
       WB:   {price, discount_price, source: 'legacy_wb_projection'}
       Ниже — единственное место, которое их различает. */
    function priceFacts(listing) {
        var summary = (listing && listing.price_summary) || {};
        var result = {
            available: summary.available !== false,
            currency: typeof summary.currency === 'string' && /^[a-z]{3}$/i.test(summary.currency)
                ? summary.currency.toUpperCase() : (summary.source === 'legacy_wb_projection' ? 'RUB' : null),
            current: null,
            before: null,
            min: null,
            base: null,
            seller: null,
            sellerPromotion: null,
            buyer: null,
            marketplaceDiscount: null,
            observedAt: listing && listing.prices_synced_at || null,
            source: null
        };
        if (!result.available) return result;
        var values = summary.values;
        if (values && typeof values === 'object') {
            // `price` is the seller-controlled amount. A missing/zero promotion
            // never replaces it, and neither field proves the buyer's price.
            result.source = 'ozon_seller_prices';
            result.seller = numberOrNull(values.price);
            result.current = result.seller;
            var basePrice = numberOrNull(values.old_price);
            result.base = basePrice !== null && basePrice > 0 ? basePrice : null;
            result.before = result.base;
            var promotion = numberOrNull(values.marketing_seller_price);
            result.sellerPromotion = promotion !== null && promotion > 0 ? promotion : null;
            result.min = numberOrNull(values.min_price);
        } else {
            // WB seller projection excludes the marketplace's buyer discounts.
            result.source = summary.source || null;
            var discounted = numberOrNull(summary.discount_price);
            var base = numberOrNull(summary.price);
            result.base = base;
            result.seller = discounted;
            result.current = discounted !== null ? discounted : base;
            result.before = discounted !== null ? base : null;
        }
        if (result.before !== null && result.current !== null
            && result.before <= result.current) {
            result.before = null;
        }
        return result;
    }

    function fmtMoney(value, currency) {
        var num = numberOrNull(value);
        if (num === null) return null;
        var suffix = !currency ? '' : currency === 'RUB' ? ' ₽' : ' ' + currency;
        return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 })
            .format(num) + suffix;
    }

    function priceLabel(listing) {
        var facts = priceFacts(listing);
        return fmtMoney(facts.current, facts.currency) || '—';
    }

    function oldPriceLabel(listing) {
        var facts = priceFacts(listing);
        return facts.before === null ? null : fmtMoney(facts.before, facts.currency);
    }

    function minPriceLabel(listing) {
        var facts = priceFacts(listing);
        return facts.min === null ? null : fmtMoney(facts.min, facts.currency);
    }

    function discountPercent(listing) {
        var facts = priceFacts(listing);
        // No generic Ozon percentage badge: old -> seller is not Ozon's subsidy.
        if (facts.source === 'ozon_seller_prices') return null;
        if (facts.before === null || facts.current === null || facts.before <= 0) {
            return null;
        }
        var pct = Math.round((facts.before - facts.current) / facts.before * 100);
        return pct >= 5 ? pct : null;
    }

    // Shared by tile, table, drawer and detail; never perform a provider read.
    var ozonPrices = {
        props: {listing: {type: Object, required: true}, compact: Boolean},
        computed: {
            facts: function () { return priceFacts(this.listing); },
            rows: function () {
                var rows = [
                    {key:'base', label:'До скидок', value:this.facts.base},
                    {key:'seller', label:'Цена продавца', value:this.facts.seller}
                ];
                if (!this.compact || this.facts.sellerPromotion !== null) rows.push({key:'sellerPromotion', label:'С вашими акциями', value:this.facts.sellerPromotion});
                rows.push({key:'buyer', label:'Для покупателя', value:null},
                    {key:'marketplaceDiscount', label:'Скидка Ozon', value:null});
                return rows;
            },
            observed: function () { return this.facts.observedAt ? relTime(this.facts.observedAt) : 'дата неизвестна'; }
        },
        methods: {
            amount: function (value) { return fmtMoney(value, this.facts.currency) || '—'; }
        },
        template: '<div class="mcat-prices" :class="{\'mcat-prices--compact\':compact}" role="group" aria-label="Цены Ozon">' +
            '<dl><div v-for="row in rows" :key="row.key" :data-price-lane="row.key" :class="{\'is-primary\':row.key===\'base\',\'is-unknown\':row.value===null}">' +
            '<dt>{{ row.label }}</dt><dd><span v-if="row.value===null" class="sr-only">Нет данных</span><span :aria-hidden="row.value===null ? \'true\' : undefined">{{ amount(row.value) }}</span></dd></div></dl>' +
            '<p v-if="!facts.currency" class="mcat-price-observed">Валюта не указана</p>' +
            '<p v-if="!facts.available" class="mcat-price-observed">Цены пока не получены</p>' +
            '<p v-if="!compact" class="mcat-price-help">Цена для покупателя и скидка Ozon пока не подтверждены. Суммы продавца показаны отдельно; цена с вашими акциями не включает дополнительную скидку площадки.</p>' +
            '<p v-if="!compact" class="mcat-price-observed">Цены обновлены: {{ observed }}</p></div>'
    };

    /* --- Остатки --------------------------------------------------------- */
    function stockNumber(listing) {
        var summary = (listing && listing.stock_summary) || null;
        if (!summary || summary.available === false) return null;
        return Number.isSafeInteger(summary.present) && summary.present >= 0 ? summary.present : null;
    }

    function stockLabel(listing) {
        var num = stockNumber(listing);
        if (num === null) return '—';
        if (num === 0) return '0 шт';
        return fmtInt(num) + ' шт';
    }

    function stockRows(listing) {
        var byType = listing && listing.stock_summary && listing.stock_summary.by_type;
        if (!byType || typeof byType !== 'object') return [];
        var rows = [];
        Object.keys(byType).forEach(function (key) {
            var entry = byType[key] || {};
            var present = stockNumber({stock_summary: entry});
            var value = present === null ? '—' : fmtInt(present);
            if (Number.isSafeInteger(entry.reserved) && entry.reserved > 0) value += ' (резерв ' + fmtInt(entry.reserved) + ')';
            rows.push({ label: String(key).toUpperCase(), value: value });
        });
        return rows;
    }

    /* --- Статусы --------------------------------------------------------- */
    function statusMeta(listing) {
        return STATUS_META[listing && listing.normalized_status] || STATUS_META.unknown;
    }

    function providerStatusMeta(listing) {
        var raw = listing && listing.provider_status;
        if (!raw) return null;
        if (raw in PROVIDER_STATUS_META) return PROVIDER_STATUS_META[raw];
        return { label: raw, tone: 'muted' };
    }

    function visibilityMeta(listing) {
        var raw = listing && listing.visibility;
        if (!raw) return null;
        if (raw in VISIBILITY_META) return VISIBILITY_META[raw];
        return { label: raw, tone: 'muted' };
    }

    function linkLabel(listing) {
        if (!listing) return '';
        if (listing.link_status === 'linked') return 'Связан';
        if (listing.link_status === 'ambiguous') return 'Конфликт';
        return 'Без связи';
    }

    function linkSourceLabel(source) {
        if (!source) return null;
        return LINK_SOURCES[source] || source;
    }

    /* --- Габариты -------------------------------------------------------- */
    function dimensionsLabel(listing) {
        var dims = (listing && listing.dimensions) || {};
        var lengthUnit = dims.dimension_unit;
        var lengthFactor = LENGTH_UNITS[lengthUnit];
        var parts = [];
        var sides = [dims.depth, dims.width, dims.height].map(numberOrNull);
        if (sides.every(function (v) { return v !== null && v > 0; })) {
            if (lengthFactor) {
                parts.push(sides.map(function (v) {
                    return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 })
                        .format(v * lengthFactor);
                }).join('×') + ' см');
            } else {
                // Неизвестная единица — показываем как есть, не выдавая за см
                parts.push(sides.join('×') + (lengthUnit ? ' ' + lengthUnit : ''));
            }
        }
        var weight = numberOrNull(dims.weight);
        if (weight !== null && weight > 0) {
            var weightFactor = WEIGHT_UNITS[dims.weight_unit];
            if (weightFactor) {
                var grams = weight * weightFactor;
                parts.push(grams >= 1000
                    ? new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 })
                        .format(grams / 1000) + ' кг'
                    : fmtInt(Math.round(grams)) + ' г');
            } else {
                parts.push(fmtInt(weight) + (dims.weight_unit ? ' ' + dims.weight_unit : ''));
            }
        }
        return parts.length ? parts.join(' · ') : null;
    }

    /* --- Прочее ---------------------------------------------------------- */
    function plural(n, one, few, many) {
        var abs = Math.abs(n) % 100;
        var last = abs % 10;
        if (abs > 10 && abs < 20) return many;
        if (last > 1 && last < 5) return few;
        if (last === 1) return one;
        return many;
    }

    function relTime(iso) {
        if (!iso) return '—';
        var normalized = /Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z';
        var date = new Date(normalized);
        if (isNaN(date.getTime())) return '—';
        var diff = Math.max(0, Date.now() - date.getTime());
        var minutes = Math.floor(diff / 60000);
        if (minutes < 1) return 'только что';
        if (minutes < 60) return minutes + ' мин назад';
        var hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + ' ч назад';
        var days = Math.floor(hours / 24);
        if (days < 7) return days + ' дн назад';
        return date.toLocaleDateString('ru-RU');
    }

    function letterOf(listing) {
        var title = ((listing && listing.title) || '').trim();
        return title ? title[0].toUpperCase() : '·';
    }

    function fallbackClass(listing) {
        var code = (listing && listing.marketplace_code) || 'wb';
        var title = (listing && listing.title) || '';
        var hash = 0;
        for (var i = 0; i < title.length; i++) {
            hash = (hash * 31 + title.charCodeAt(i)) % 997;
        }
        return ['mcat-fallback--' + code, 'mcat-fallback--v' + (hash % 4)];
    }

    /* Единая обработка ответа: истёкшая сессия не должна выглядеть как «пусто» */
    function readJson(response) {
        if (response.status === 401) {
            var authError = new Error('Сессия истекла — войдите заново, чтобы продолжить');
            authError.code = 'auth_required';
            return Promise.reject(authError);
        }
        var contentType = response.headers.get('content-type') || '';
        if (contentType.indexOf('application/json') === -1) {
            if (response.redirected || contentType.indexOf('text/html') !== -1) {
                var htmlError = new Error('Сессия истекла — войдите заново, чтобы продолжить');
                htmlError.code = 'auth_required';
                return Promise.reject(htmlError);
            }
        }
        return response.json().catch(function () {
            throw new Error('Сервер вернул непонятный ответ. Данные не обновлены — повторите загрузку.');
        }).then(function (data) {
            if (!response.ok || data.success === false) {
                throw new Error(
                    data.error || ('Не удалось загрузить данные (HTTP ' + response.status + ')')
                );
            }
            return data;
        });
    }

    // A stalled upstream image must eventually use the existing error fallback.
    // Lazy images get their deadline only when near the viewport.
    var imageStates = new WeakMap();
    function stopImageDeadline(el) {
        var state = imageStates.get(el);
        if (!state) return;
        clearTimeout(state.timer);
        if (state.observer) state.observer.disconnect();
        el.removeEventListener('load', state.done);
        el.removeEventListener('error', state.done);
        imageStates.delete(el);
    }
    function watchImage(el) {
        var src = el.getAttribute('src');
        var previous = imageStates.get(el);
        if (previous && previous.src === src) return;
        stopImageDeadline(el);
        if (!src || (el.complete && el.naturalWidth > 0)) return;
        var state = {src: src, timer: null, observer: null};
        state.done = function () { stopImageDeadline(el); };
        imageStates.set(el, state);
        el.addEventListener('load', state.done);
        el.addEventListener('error', state.done);
        var start = function () {
            if (state.observer) state.observer.disconnect();
            if (imageStates.get(el) !== state || state.timer !== null) return;
            state.timer = setTimeout(function () {
                if (imageStates.get(el) !== state || el.getAttribute('src') !== src) return;
                stopImageDeadline(el);
                if (el.complete && el.naturalWidth > 0) return;
                el.removeAttribute('src');
                el.dispatchEvent(new Event('error'));
            }, 12000);
        };
        if (el.loading === 'lazy' && typeof global.IntersectionObserver === 'function') {
            state.observer = new global.IntersectionObserver(function (entries) {
                if (entries.some(function (entry) { return entry.isIntersecting; })) start();
            }, {rootMargin: '300px'});
            state.observer.observe(el);
        } else start();
    }

    global.mcatShared = {
        imageDeadline: {mounted: watchImage, updated: watchImage, beforeUnmount: function (el) {
            stopImageDeadline(el);
            el.removeAttribute('src');
        }},
        STATUS_META: STATUS_META,
        priceFacts: priceFacts,
        ozonPrices: ozonPrices,
        fmtMoney: fmtMoney,
        fmtInt: fmtInt,
        priceLabel: priceLabel,
        oldPriceLabel: oldPriceLabel,
        minPriceLabel: minPriceLabel,
        discountPercent: discountPercent,
        stockNumber: stockNumber,
        stockLabel: stockLabel,
        stockRows: stockRows,
        statusMeta: statusMeta,
        providerStatusMeta: providerStatusMeta,
        visibilityMeta: visibilityMeta,
        linkLabel: linkLabel,
        linkSourceLabel: linkSourceLabel,
        dimensionsLabel: dimensionsLabel,
        plural: plural,
        relTime: relTime,
        letterOf: letterOf,
        fallbackClass: fallbackClass,
        readJson: readJson
    };
})(window);
