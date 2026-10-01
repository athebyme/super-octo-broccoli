(function (global) {
    'use strict';

    function selectionUrl(base, productIds) {
        if (typeof base !== 'string' || base.charAt(0) !== '/' || base.indexOf('//') === 0) {
            throw new TypeError('A same-site common-content path is required.');
        }
        if (!Array.isArray(productIds)) throw new TypeError('Product selection must be a list.');
        var ids = [];
        productIds.forEach(function (value) {
            if (!Number.isSafeInteger(value) || value <= 0) throw new TypeError('Product IDs must be positive integers.');
            if (ids.indexOf(value) === -1) ids.push(value);
        });
        if (!ids.length) throw new RangeError('Choose at least one product.');
        if (ids.length > 50) throw new RangeError('Choose no more than 50 products.');
        var query = new URLSearchParams();
        ids.forEach(function (id) { query.append('product_id', String(id)); });
        return base + '?' + query.toString();
    }

    function clone(value) {
        if (value === undefined) return undefined;
        return JSON.parse(JSON.stringify(value));
    }

    function stable(value) {
        if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
        if (value && typeof value === 'object') {
            return '{' + Object.keys(value).sort().map(function (key) {
                return JSON.stringify(key) + ':' + stable(value[key]);
            }).join(',') + '}';
        }
        return JSON.stringify(value);
    }

    function serializeFieldChange(mode, value, isOverridden, effectiveValue) {
        if (mode === 'inherit') return isOverridden ? { mode: 'inherit' } : null;
        if (mode !== 'override') throw new TypeError('Unknown common-content field mode.');
        if (isOverridden && stable(value) === stable(effectiveValue)) return null;
        return { mode: 'override', value: clone(value) };
    }

    function readablePhotoOrder(value, photoOptions) {
        if (!Array.isArray(value) || !value.length) return 'Фотографии не выбраны';
        var options = Array.isArray(photoOptions) ? photoOptions : [];
        return value.map(function (url) {
            var index = options.findIndex(function (option) { return option && option.url === url; });
            return index >= 0 ? 'Фото ' + (index + 1) : 'Выбранное фото';
        }).join(' → ');
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            selectionUrl: selectionUrl,
            serializeFieldChange: serializeFieldChange,
            readablePhotoOrder: readablePhotoOrder,
        };
    }
    if (global) {
        global.SellerHubCommonContent = global.SellerHubCommonContent || {};
        global.SellerHubCommonContent.selectionUrl = selectionUrl;
        global.SellerHubCommonContent.serializeFieldChange = serializeFieldChange;
        global.SellerHubCommonContent.readablePhotoOrder = readablePhotoOrder;
    }
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : null));

(function () {
    'use strict';

    if (typeof document === 'undefined') return;

    var root = document.getElementById('common-content-editor');
    var bootstrapNode = document.getElementById('common-content-bootstrap');
    if (!root || !bootstrapNode) return;

    var bootstrap;
    try { bootstrap = JSON.parse(bootstrapNode.textContent || '{}'); }
    catch (_) { return; }

    var labels = {
        title: 'Название',
        description: 'Описание',
        photos: 'Фотографии',
        characteristics: 'Характеристики',
    };
    var helpers = window.SellerHubCommonContent || {};
    var originLabels = {
        source: 'из источника товара',
        supplier_enrichment: 'из дополнения поставщика',
        ai_suggestion: 'из предложения AI',
        seller_override: 'введено продавцом',
        unknown: 'источник не указан',
    };
    var sourceTypeLabels = {
        csv: 'импорт из файла',
        manual: 'ручной ввод',
        synthetic: 'локальный источник',
        unknown: 'источник не указан',
        sexoptovik: 'каталог SexOptovik',
    };
    var recipientStatusLabels = {
        active: 'Активна',
        archived: 'В архиве',
        draft: 'Черновик',
        linked: 'Связана',
        needs_attributes: 'Нужно заполнить характеристики',
        needs_category: 'Нужна категория',
        ready: 'Готова к проверке',
        unknown: 'Неизвестно',
    };
    var state = {
        records: [],
        byId: new Map(),
        currentId: null,
        preview: null,
        previewToken: null,
        acknowledged: false,
        busy: false,
        requiresRefresh: false,
    };

    var listNode = document.getElementById('common-content-product-list');
    var fieldsNode = document.getElementById('common-content-fields');
    var previewNode = document.getElementById('common-content-preview');
    var errorNode = document.getElementById('common-content-error');
    var statusNode = document.getElementById('common-content-status');

    function clone(value) {
        if (value === undefined) return undefined;
        return JSON.parse(JSON.stringify(value));
    }

    function stable(value) {
        if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
        if (value && typeof value === 'object') {
            return '{' + Object.keys(value).sort().map(function (key) {
                return JSON.stringify(key) + ':' + stable(value[key]);
            }).join(',') + '}';
        }
        return JSON.stringify(value);
    }

    function same(left, right) { return stable(left) === stable(right); }

    function node(tag, className, text) {
        var item = document.createElement(tag);
        if (className) item.className = className;
        if (text !== undefined && text !== null) item.textContent = String(text);
        return item;
    }

    function button(text, action, className) {
        var item = node('button', className || 'cpc-quiet-button', text);
        item.type = 'button';
        item.dataset.action = action;
        return item;
    }

    function originLabel(value) { return originLabels[value] || originLabels.unknown; }

    function sourceLabel(source) {
        var type = source && source.type ? String(source.type) : 'unknown';
        var identity = source && source.identity ? source.identity : {};
        var supplierProductId = source && source.supplier_product_id || identity.supplier_product_id;
        var label = Number(supplierProductId) > 0
            ? 'карточка поставщика'
            : (sourceTypeLabels[type] || 'источник товара');
        var revision = source && source.revision !== null && source.revision !== undefined
            ? ' · версия источника ' + source.revision : '';
        return label + revision;
    }

    function recipientStatusLabel(status) {
        var value = String(status || 'unknown');
        return recipientStatusLabels[value] || 'Состояние не указано';
    }

    function readable(value, field, record) {
        if (value === null || value === undefined || value === '') return 'Пусто';
        if (field === 'photos' && Array.isArray(value)) {
            return helpers.readablePhotoOrder(value, record && record.state
                ? record.state.photo_options : []);
        }
        if (field === 'characteristics' && Array.isArray(value)) {
            if (!value.length) return 'Характеристики не заполнены';
            return value.map(function (row) {
                var name = row && typeof row.name === 'string' ? row.name : 'Свойство';
                return name + ': ' + readableCharacteristic(row && row.value);
            }).join(' · ');
        }
        return String(value);
    }

    function appendDescriptionDisclosure(parent, value, label) {
        var text = typeof value === 'string' ? value : '';
        var details = node('details', 'cpc-description-disclosure');
        var summary = node('summary', '', label + ' · ' + text.length + ' символов');
        var full = node('div', 'cpc-description-full', text || 'Описание пустое.');
        full.tabIndex = 0;
        details.append(summary, full);
        parent.appendChild(details);
    }

    function appendDiffValue(parent, value, field, record) {
        if (field !== 'description') {
            parent.appendChild(node('span', '', readable(value, field, record)));
            return;
        }
        var text = typeof value === 'string' ? value : '';
        parent.appendChild(node('span', 'cpc-description-summary', text
            ? text.slice(0, 220) + (text.length > 220 ? '…' : '')
            : 'Пустое описание'));
        appendDescriptionDisclosure(parent, text, 'Полное описание');
    }

    function readableCharacteristic(value, depth) {
        depth = depth || 0;
        if (value === null || value === undefined || value === '') return 'пусто';
        if (Array.isArray(value)) return value.slice(0, 20).map(function (item) {
            return readableCharacteristic(item, depth + 1);
        }).join(', ');
        if (value && typeof value === 'object') {
            if (depth > 1) return 'составное значение';
            return Object.keys(value).slice(0, 8).map(function (key) {
                return key + ': ' + readableCharacteristic(value[key], depth + 1);
            }).join(', ') || 'составное значение';
        }
        return String(value);
    }

    function photoName(url, record) {
        var options = record && record.state && Array.isArray(record.state.photo_options)
            ? record.state.photo_options : [];
        var index = options.findIndex(function (option) { return option && option.url === url; });
        return index >= 0 ? 'Фото ' + (index + 1) : 'Выбранное фото';
    }

    function makeRecord(product) {
        var draftFields = {};
        Object.keys(labels).forEach(function (field) {
            var original = product.fields && product.fields[field] ? product.fields[field] : {};
            draftFields[field] = {
                mode: original.is_overridden ? 'override' : 'inherit',
                value: clone(original.effective),
            };
        });
        return {
            state: product,
            draftFields: draftFields,
            recipientKeys: new Set(),
            characteristicRows: null,
        };
    }

    function fieldChange(record, field) {
        var original = record.state.fields[field] || {};
        var draft = record.draftFields[field];
        if (!draft) return null;
        var value = field === 'characteristics'
            ? characteristicsValue(record)
            : draft.value;
        return helpers.serializeFieldChange(draft.mode, value, !!original.is_overridden, original.effective);
    }

    function isDirty(record) {
        return Object.keys(labels).some(function (field) {
            return fieldChange(record, field) !== null;
        });
    }

    function selectedRecipients(record) {
        return (record.state.recipients || []).filter(function (recipient) {
            return record.recipientKeys.has(recipientKey(recipient.ref));
        });
    }

    function recipientKey(ref) { return ref.kind + ':' + ref.id; }

    function showError(message) {
        if (!errorNode) return;
        errorNode.textContent = message || 'Не удалось выполнить действие. Обновите страницу и проверьте товар.';
        errorNode.hidden = false;
        errorNode.focus();
    }

    function clearError() {
        if (!errorNode) return;
        errorNode.textContent = '';
        errorNode.hidden = true;
    }

    function showStatus(message) {
        if (!statusNode) return;
        statusNode.textContent = message || '';
        statusNode.hidden = !message;
    }

    function discardPreview() {
        state.preview = null;
        state.previewToken = null;
        state.acknowledged = false;
        if (fieldsNode) fieldsNode.hidden = false;
        if (previewNode) {
            previewNode.replaceChildren();
            previewNode.hidden = true;
        }
    }

    function createModeButton(record, field) {
        var draft = record.draftFields[field];
        var item = button(
            draft.mode === 'inherit' ? 'Изменить значение' : 'Вернуть к источнику',
            'toggle-mode',
            'cpc-mode-button',
        );
        item.dataset.field = field;
        item.setAttribute('aria-pressed', draft.mode === 'override' ? 'true' : 'false');
        return item;
    }

    function makeFieldSection(record, field) {
        var original = record.state.fields[field] || {};
        var draft = record.draftFields[field];
        var section = node('section', 'cpc-field');
        section.dataset.fieldSection = field;

        var head = node('div', 'cpc-field-head');
        var titleWrap = node('div');
        titleWrap.appendChild(node('h3', '', labels[field]));
        titleWrap.appendChild(node(
            'p', 'cpc-origin',
            'Сейчас: ' + originLabel(original.origin) + '. Наследуемое значение: ' + originLabel(original.inherited_origin) + '.',
        ));
        head.appendChild(titleWrap);
        head.appendChild(createModeButton(record, field));
        section.appendChild(head);

        var body = node('div', 'cpc-field-body');
        if (field === 'photos') body.appendChild(renderPhotos(record, draft));
        else if (field === 'characteristics') body.appendChild(renderCharacteristics(record, draft));
        else body.appendChild(renderTextField(field, draft, original));

        var hint = node('div', 'cpc-field-hint');
        if (field === 'description' && draft.mode === 'inherit') {
            hint.appendChild(node('span', '', 'Используется описание из ' + originLabel(original.inherited_origin) + '.'));
            appendDescriptionDisclosure(hint, original.inherited, 'Показать полное наследуемое описание');
        } else {
            hint.textContent = draft.mode === 'inherit'
                ? 'Используется текущее наследуемое значение: ' + readable(original.inherited, field, record)
                : 'Своё значение останется в общем товаре и не обновит карточки площадок.';
        }
        body.appendChild(hint);
        section.appendChild(body);
        return section;
    }

    function renderTextField(field, draft, original) {
        var input = field === 'description' ? node('textarea', 'cpc-value-input') : node('input', 'cpc-value-input');
        input.dataset.fieldInput = field;
        input.value = draft.mode === 'inherit'
            ? String(original.inherited || '')
            : String(draft.value || '');
        input.disabled = draft.mode === 'inherit';
        if (field === 'title') {
            input.type = 'text';
            input.maxLength = 500;
            input.required = draft.mode === 'override';
            input.setAttribute('aria-label', 'Общее название товара');
        } else {
            input.rows = 5;
            input.maxLength = 100000;
            input.setAttribute('aria-label', 'Общее описание товара');
        }
        return input;
    }

    function photoList(record) {
        var draft = record.draftFields.photos;
        if (!Array.isArray(draft.value)) draft.value = [];
        return draft.value;
    }

    function renderPhotos(record, draft) {
        var wrap = node('div', 'cpc-photo-editor');
        var currentPhotos = Array.isArray(draft.value) ? draft.value : [];
        var options = Array.isArray(record.state.photo_options) ? record.state.photo_options : [];
        var grid = node('div', 'cpc-photo-options');
        if (!options.length) {
            grid.appendChild(node('p', 'cpc-field-hint', 'Доступных фотографий нет. Добавить произвольную ссылку здесь нельзя.'));
        }
        options.forEach(function (option, index) {
            var selected = currentPhotos.indexOf(option.url) !== -1;
            var available = !!option.available_for_selection || selected;
            var item = node('button', 'cpc-photo-option' + (selected ? ' is-selected' : ''));
            item.type = 'button';
            item.dataset.action = 'toggle-photo';
            item.dataset.photoUrl = option.url;
            item.dataset.field = 'photos';
            item.disabled = draft.mode === 'inherit' || (!available && !selected);
            item.setAttribute('aria-pressed', selected ? 'true' : 'false');
            item.setAttribute('aria-disabled', item.disabled ? 'true' : 'false');
            item.setAttribute('aria-label', photoName(option.url, record) + ', ' + (selected ? 'выбрано' : 'не выбрано') + ', источник: ' + (option.source || 'общий товар'));

            var image = node('span', 'cpc-photo-preview');
            if (option.preview_url) {
                var img = document.createElement('img');
                img.src = option.preview_url;
                img.alt = '';
                img.loading = 'lazy';
                img.decoding = 'async';
                img.addEventListener('error', function () {
                    image.replaceChildren(node('span', '', 'Предпросмотр недоступен'));
                }, { once: true });
                image.appendChild(img);
            } else {
                image.appendChild(node('span', '', selected ? 'В текущем выборе' : 'Нет предпросмотра'));
            }
            item.appendChild(image);
            item.appendChild(node('span', 'cpc-photo-name', photoName(option.url, record)));
            item.appendChild(node('span', 'cpc-photo-source', option.source || 'общий товар'));
            if (!available && !selected) item.appendChild(node('span', 'cpc-photo-source', 'Недоступно для выбора'));
            grid.appendChild(item);
        });
        wrap.appendChild(grid);

        var order = node('div', 'cpc-photo-order');
        order.appendChild(node('h4', '', 'Порядок фотографий'));
        if (!currentPhotos.length) order.appendChild(node('span', 'cpc-field-hint', 'Выберите фото из списка выше.'));
        currentPhotos.forEach(function (url, index) {
            var row = node('div', 'cpc-photo-order-row');
            row.appendChild(node('span', 'cpc-photo-order-name', photoName(url, record) + ' · ' + (findPhoto(record, url).source || 'общий товар')));
            var actions = node('span', 'cpc-photo-order-actions');
            var up = button('↑', 'move-photo', 'cpc-photo-move');
            up.dataset.photoUrl = url;
            up.dataset.direction = '-1';
            up.disabled = draft.mode === 'inherit' || index === 0;
            up.setAttribute('aria-label', 'Переместить ' + photoName(url, record) + ' вверх');
            var down = button('↓', 'move-photo', 'cpc-photo-move');
            down.dataset.photoUrl = url;
            down.dataset.direction = '1';
            down.disabled = draft.mode === 'inherit' || index === currentPhotos.length - 1;
            down.setAttribute('aria-label', 'Переместить ' + photoName(url, record) + ' вниз');
            actions.append(up, down);
            row.appendChild(actions);
            order.appendChild(row);
        });
        wrap.appendChild(order);
        return wrap;
    }

    function findPhoto(record, url) {
        return (record.state.photo_options || []).find(function (option) { return option.url === url; }) || {};
    }

    function characteristicRows(record) {
        if (Array.isArray(record.characteristicRows)) return record.characteristicRows;
        var value = record.draftFields.characteristics.value;
        record.characteristicRows = (Array.isArray(value) ? value : []).map(function (row) {
            var item = row && typeof row === 'object' ? row : { name: '', value: row };
            var originalValue = clone(item.value);
            return {
                name: typeof item.name === 'string' ? item.name : '',
                value: originalValue,
                initialValue: clone(originalValue),
                initialDisplay: readableCharacteristic(originalValue),
                valueText: readableCharacteristic(originalValue),
            };
        });
        return record.characteristicRows;
    }

    function characteristicsValue(record) {
        return characteristicRows(record).map(function (row) {
            return { name: row.name.trim(), value: clone(row.value) };
        });
    }

    function renderCharacteristics(record, draft) {
        var wrap = node('div', 'cpc-characteristics');
        var rows = characteristicRows(record);
        var list = node('div', 'cpc-char-list');
        if (!rows.length) list.appendChild(node('p', 'cpc-field-hint', 'Характеристики не заполнены. Добавляйте понятные названия и значения, без кодов площадок.'));
        rows.forEach(function (row, index) {
            var item = node('div', 'cpc-char-row');
            var name = node('input', 'cpc-value-input cpc-char-name');
            name.type = 'text';
            name.maxLength = 500;
            name.value = row.name;
            name.disabled = draft.mode === 'inherit';
            name.dataset.charIndex = String(index);
            name.dataset.charPart = 'name';
            name.setAttribute('aria-label', 'Название характеристики ' + (index + 1));
            var value = node('input', 'cpc-value-input cpc-char-value');
            value.type = 'text';
            value.maxLength = 2000;
            value.value = row.valueText;
            value.disabled = draft.mode === 'inherit';
            value.dataset.charIndex = String(index);
            value.dataset.charPart = 'value';
            value.setAttribute('aria-label', 'Значение характеристики ' + (index + 1));
            var remove = button('Убрать', 'remove-characteristic', 'cpc-char-remove');
            remove.dataset.charIndex = String(index);
            remove.disabled = draft.mode === 'inherit';
            remove.setAttribute('aria-label', 'Убрать характеристику ' + (index + 1));
            item.append(name, value, remove);
            list.appendChild(item);
        });
        wrap.appendChild(list);
        var add = button('Добавить характеристику', 'add-characteristic', 'cpc-quiet-button cpc-char-add');
        add.disabled = draft.mode === 'inherit';
        wrap.appendChild(add);
        return wrap;
    }

    function renderRecipients(record) {
        var section = node('section', 'cpc-recipient-section');
        section.appendChild(node('h3', '', 'Контексты каналов для сравнения'));
        section.appendChild(node('p', '', 'Выберите карточки, чтобы увидеть различия до и после. Это только просмотр: сохранение не меняет эти карточки и не запускает отправку.'));
        var rows = Array.isArray(record.state.recipients) ? record.state.recipients : [];
        var list = node('div', 'cpc-recipient-list');
        if (!rows.length) list.appendChild(node('p', 'cpc-field-hint', 'Связанных карточек каналов не найдено.'));
        rows.forEach(function (recipient) {
            var ref = recipient.ref || {};
            var key = recipientKey(ref);
            var label = node('label', 'cpc-recipient');
            var checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.checked = record.recipientKeys.has(key);
            checkbox.dataset.recipientKey = key;
            checkbox.dataset.recipientKind = ref.kind || '';
            checkbox.dataset.recipientId = String(ref.id || '');
            checkbox.setAttribute('aria-label', 'Сравнить с ' + (recipient.channel_label || recipient.channel || 'каналом') + ', ' + (recipient.account_label || 'кабинетом'));
            var copy = node('span');
            var title = node('strong', '', (recipient.channel_label || recipient.channel || 'Канал') + ' · ' + (recipient.account_label || 'без кабинета'));
            copy.appendChild(title);
            copy.appendChild(node('small', '', 'Состояние: ' + recipientStatusLabel(recipient.status) + ' · версия ' + (recipient.version || '—')));
            if (typeof recipient.href === 'string' && recipient.href.charAt(0) === '/') {
                var link = node('a', '', 'Открыть карточку отдельно');
                link.href = recipient.href;
                link.target = '_blank';
                link.rel = 'noopener';
                link.addEventListener('click', function (event) { event.stopPropagation(); });
                copy.appendChild(link);
            }
            label.append(checkbox, copy);
            list.appendChild(label);
        });
        section.appendChild(list);
        if (record.state.recipients_truncated) {
            section.appendChild(node('p', 'cpc-recipient-truncated', 'Показаны только первые контексты. Список ограничен для безопасного просмотра.'));
        }
        return section;
    }

    function renderActions(record) {
        var wrap = node('div', 'cpc-actions');
        var group = node('div', 'cpc-actions-group');
        var reset = button('Отменить правки товара', 'reset-product', 'sh-btn sh-btn--secondary');
        reset.disabled = !isDirty(record) || state.busy;
        group.appendChild(reset);
        var refresh = button('Перечитать выбранные', 'refresh-product', 'cpc-quiet-button');
        refresh.disabled = state.busy;
        group.appendChild(refresh);
        wrap.appendChild(group);
        var review = button('Проверить изменения', 'preview', 'sh-btn sh-btn--primary');
        review.disabled = state.requiresRefresh || !state.records.some(isDirty) || state.busy;
        wrap.appendChild(review);
        return wrap;
    }

    function renderCurrent() {
        if (!fieldsNode || !state.currentId) return;
        clearError();
        var record = state.byId.get(state.currentId);
        if (!record) return;
        fieldsNode.replaceChildren();
        var heading = node('header', 'cpc-product-heading');
        var name = node('div');
        name.appendChild(node('h2', '', record.state.title || 'Без названия'));
        name.appendChild(node('p', '', 'Артикул ' + (record.state.external_id || 'не указан') + ' · версия общего товара ' + record.state.content_edit_version));
        heading.appendChild(name);
        heading.appendChild(node('p', 'cpc-source-line', sourceLabel(record.state.source || {})));
        fieldsNode.appendChild(heading);
        Object.keys(labels).forEach(function (field) {
            fieldsNode.appendChild(makeFieldSection(record, field));
        });
        fieldsNode.appendChild(renderRecipients(record));
        fieldsNode.appendChild(renderActions(record));
        renderProductList();
    }

    function renderProductList() {
        if (!listNode) return;
        listNode.replaceChildren();
        state.records.forEach(function (record) {
            var item = node('li');
            var choose = node('button');
            choose.type = 'button';
            choose.dataset.action = 'choose-product';
            choose.dataset.productId = String(record.state.product_id);
            choose.setAttribute('aria-current', record.state.product_id === state.currentId ? 'true' : 'false');
            choose.appendChild(node('span', 'cpc-product-name', record.state.title || 'Без названия'));
            choose.appendChild(node('span', 'cpc-product-meta', 'Артикул ' + (record.state.external_id || 'не указан')));
            if (isDirty(record)) choose.appendChild(node('span', 'cpc-product-dirty', 'Есть несохранённые правки'));
            item.appendChild(choose);
            listNode.appendChild(item);
        });
    }

    function renderPreview() {
        if (!previewNode || !state.preview) return;
        if (fieldsNode) fieldsNode.hidden = true;
        previewNode.replaceChildren();
        previewNode.hidden = false;
        var wrap = node('section', 'cpc-preview');
        wrap.setAttribute('aria-labelledby', 'cpc-preview-title');
        wrap.appendChild(node('h3', '', 'Проверьте изменения общего товара'));
        wrap.lastChild.id = 'cpc-preview-title';
        wrap.appendChild(node('p', 'cpc-preview-intro', 'После подтверждения изменятся только сведения в Seller Hub. Черновики и карточки каналов останутся как сейчас.'));
        (state.preview.items || []).forEach(function (item) {
            var record = state.byId.get(item.product_id);
            var card = node('section', 'cpc-diff-product');
            card.appendChild(node('h4', '', record ? record.state.title : 'Выбранный товар'));
            (item.fields || []).forEach(function (field) {
                var row = node('div', 'cpc-diff-row');
                row.appendChild(node('span', 'cpc-diff-label', labels[field.field] || field.field));
                var before = node('div', 'cpc-diff-value');
                before.appendChild(node('small', '', 'Сейчас · ' + originLabel(field.before_origin)));
                appendDiffValue(before, field.before, field.field, record);
                var after = node('div', 'cpc-diff-value cpc-diff-after');
                after.appendChild(node('small', '', 'После · ' + originLabel(field.after_origin)));
                appendDiffValue(after, field.after, field.field, record);
                row.append(before, after);
                card.appendChild(row);
            });
            (item.recipients || []).forEach(function (recipient) {
                var diff = node('div', 'cpc-diff-recipient');
                diff.appendChild(node('strong', '', (recipient.channel_label || recipient.channel || 'Канал') + ' · ' + (recipient.account_label || 'без кабинета')));
                (recipient.diff || []).forEach(function (entry) {
                    diff.appendChild(node('p', '', (labels[entry.field] || entry.field) + ': ' + (entry.message || (entry.matches ? 'значение совпадает' : 'значение в карточке канала не меняется'))));
                });
                diff.appendChild(node('p', '', recipient.notice || 'Карточка останется без изменений.'));
                if (typeof recipient.href === 'string' && recipient.href.charAt(0) === '/') {
                    var link = node('a', '', 'Открыть карточку отдельно');
                    link.href = recipient.href;
                    link.target = '_blank';
                    link.rel = 'noopener';
                    diff.appendChild(link);
                }
                card.appendChild(diff);
            });
            wrap.appendChild(card);
        });
        var label = node('label', 'cpc-preview-confirm');
        var acknowledge = document.createElement('input');
        acknowledge.type = 'checkbox';
        acknowledge.checked = state.acknowledged;
        acknowledge.dataset.action = 'acknowledge-preview';
        label.appendChild(acknowledge);
        label.appendChild(node('span', '', 'Я проверил diff. Сохранится общий товар в Seller Hub; карточки площадок не обновятся.'));
        wrap.appendChild(label);
        var actions = node('div', 'cpc-preview-actions');
        actions.appendChild(button('Вернуться к полям', 'back-to-fields', 'sh-btn sh-btn--secondary'));
        var apply = button(state.busy ? 'Сохраняем…' : 'Сохранить общий товар', 'apply', 'sh-btn sh-btn--primary');
        apply.disabled = !state.acknowledged || state.busy || !state.previewToken;
        actions.appendChild(apply);
        wrap.appendChild(actions);
        previewNode.appendChild(wrap);
        acknowledge.focus({ preventScroll: true });
    }

    function requestItems() {
        var items = [];
        var totalRecipients = 0;
        state.records.forEach(function (record) {
            var changes = {};
            Object.keys(labels).forEach(function (field) {
                var change = fieldChange(record, field);
                if (change) changes[field] = change;
            });
            if (!Object.keys(changes).length) return;
            if (changes.title) {
                var titleValue = changes.title.mode === 'inherit'
                    ? record.state.fields.title.inherited
                    : changes.title.value;
                if (typeof titleValue !== 'string' || !titleValue.trim()) {
                    throw new Error('Заполните название товара или оставьте текущее значение источника.');
                }
            }
            var recipients = selectedRecipients(record).map(function (recipient) {
                return clone(recipient.ref);
            });
            totalRecipients += recipients.length;
            items.push({
                product_id: record.state.product_id,
                expected_content_edit_version: record.state.content_edit_version,
                changes: changes,
                recipients: recipients,
            });
        });
        if (!items.length) throw new Error('Сначала измените поле или верните переопределение к источнику.');
        if (items.length > 50) throw new Error('За один просмотр можно изменить не больше 50 товаров. Разделите выбор.');
        if (totalRecipients > 100) throw new Error('Для сравнения выбрано больше 100 каналов. Снимите часть отметок и проверьте изменения снова.');
        return items;
    }

    async function boundedJsonRequest(url, options) {
        var controller = typeof AbortController === 'function' ? new AbortController() : null;
        var timeout = controller ? window.setTimeout(function () { controller.abort(); }, 15000) : null;
        try {
            var response = await fetch(url, Object.assign({}, options, controller ? { signal: controller.signal } : {}));
            var data;
            try { data = await response.json(); }
            catch (_) { data = {}; }
            return { response: response, data: data };
        } catch (error) {
            if (error && error.name === 'AbortError') {
                var timeoutError = new Error('Запрос занял больше 15 секунд. Обновите данные товара и проверьте его текущее состояние.');
                timeoutError.code = 'request_timeout';
                throw timeoutError;
            }
            throw error;
        } finally {
            if (timeout !== null) window.clearTimeout(timeout);
        }
    }

    async function postJson(url, body) {
        var result = await boundedJsonRequest(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': bootstrap.csrfToken,
            },
            body: JSON.stringify(body),
        });
        var response = result.response;
        var data = result.data;
        if (!response.ok || !data.success) {
            var error = new Error(data.error || 'Не удалось проверить общий товар. Обновите страницу и повторите проверку.');
            error.status = response.status;
            error.code = data.code || '';
            throw error;
        }
        return data;
    }

    async function getProduct(productId) {
        var result = await boundedJsonRequest(bootstrap.readUrlBase + encodeURIComponent(productId) + '/common-content', {
            method: 'GET',
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' },
        });
        var response = result.response;
        var data = result.data;
        if (!response.ok || !data.success || !data.product) {
            throw new Error(data.error || 'Не удалось обновить данные товара. Перезагрузите страницу.');
        }
        return data.product;
    }

    async function preview() {
        if (state.busy) return;
        if (state.requiresRefresh) {
            showError('Сначала перечитайте выбранные товары, затем проверьте diff заново.');
            return;
        }
        clearError();
        showStatus('');
        discardPreview();
        var failureMessage = '';
        try {
            var items = requestItems();
            setBusy(true);
            var response = await postJson(bootstrap.previewUrl, { items: items });
            if (response.save_effect !== 'common_only') {
                throw new Error('Сервер вернул неизвестный результат. Сохранение остановлено.');
            }
            state.preview = response.preview;
            state.previewToken = response.preview.preview_token;
            state.acknowledged = false;
            renderPreview();
        } catch (error) {
            if (error && (error.status === 409 || error.code === 'common_content_conflict')) {
                state.requiresRefresh = true;
                discardPreview();
                failureMessage = 'Данные товара или карточки канала изменились. Предыдущая проверка отменена. Перечитайте выбранные товары и проверьте diff заново.';
            } else {
                failureMessage = (error && error.message) || 'Не удалось проверить изменения. Обновите страницу и повторите проверку.';
            }
        } finally {
            setBusy(false);
            if (state.preview) renderPreview();
            if (fieldsNode) {
                var reviewButton = fieldsNode.querySelector('[data-action="preview"]');
                if (reviewButton) reviewButton.disabled = state.requiresRefresh || !state.records.some(isDirty) || state.busy;
            }
            if (failureMessage) showError(failureMessage);
        }
    }

    async function applyPreview() {
        if (!state.previewToken || !state.acknowledged || state.busy) return;
        clearError();
        var previewSnapshot = clone(state.preview);
        var previewToken = state.previewToken;
        var failureMessage = '';
        setBusy(true);
        try {
            var response = await postJson(bootstrap.applyUrl, { preview_token: previewToken });
            if (response.save_effect !== 'common_only' || !Array.isArray(response.applied)) {
                throw new Error('Сервер не подтвердил сохранение общего товара. Проверьте данные перед повтором.');
            }
            var changedIds = response.applied.map(function (row) { return row.product_id; });
            response.applied.forEach(function (applied) {
                var previous = state.byId.get(applied.product_id);
                var product = previous ? clone(previous.state) : null;
                var previewItem = (previewSnapshot.items || []).find(function (item) {
                    return item.product_id === applied.product_id;
                });
                if (!product || !previewItem) return;
                product.content_edit_version = applied.content_edit_version;
                (previewItem.fields || []).forEach(function (field) {
                    var current = product.fields[field.field] || {};
                    current.effective = clone(field.after);
                    current.origin = field.after_origin;
                    current.inherited = clone(field.inherited);
                    current.inherited_origin = field.after_origin === 'seller_override'
                        ? current.inherited_origin
                        : field.after_origin;
                    current.is_overridden = field.after_origin === 'seller_override';
                    product.fields[field.field] = current;
                    if (field.field === 'title') product.title = String(field.after || 'Без названия');
                });
                var fresh = makeRecord(product);
                if (previous) fresh.recipientKeys = new Set(previous.recipientKeys);
                state.byId.set(applied.product_id, fresh);
                state.records = state.records.map(function (record) { return record.state.product_id === applied.product_id ? fresh : record; });
            });
            state.currentId = changedIds[0] || state.currentId;
            discardPreview();
            renderCurrent();
            showStatus(response.notice || 'Общий товар сохранён. Карточки каналов не изменены.');
            try {
                var refreshed = await Promise.all(changedIds.map(function (productId) {
                    return getProduct(productId).then(function (product) { return [productId, product]; });
                }));
                refreshed.forEach(function (pair) {
                    var previous = state.byId.get(pair[0]);
                    var fresh = makeRecord(pair[1]);
                    if (previous) fresh.recipientKeys = new Set(previous.recipientKeys);
                    state.byId.set(pair[0], fresh);
                    state.records = state.records.map(function (record) { return record.state.product_id === pair[0] ? fresh : record; });
                });
                state.requiresRefresh = false;
                renderCurrent();
            } catch (_) {
                state.requiresRefresh = true;
                showStatus('Общий товар сохранён, но обновить отображение не удалось. Перезагрузите страницу перед следующей правкой. Карточки каналов не изменены.');
            }
        } catch (error) {
            discardPreview();
            state.requiresRefresh = true;
            if (error && error.status === 409) {
                failureMessage = 'Товар или карточка канала изменились. Предыдущий diff отменён. Обновите данные товара и проверьте изменения заново.';
            } else {
                failureMessage = 'Ответ о сохранении не удалось подтвердить. Не повторяйте действие: перечитайте общий товар и проверьте его текущее состояние.';
            }
        } finally {
            setBusy(false);
            renderCurrent();
            if (failureMessage) showError(failureMessage);
        }
    }

    function setBusy(value) {
        state.busy = !!value;
        if (root) {
            root.setAttribute('aria-busy', state.busy ? 'true' : 'false');
            root.inert = state.busy;
        }
        if (fieldsNode) fieldsNode.inert = state.busy;
        if (previewNode) previewNode.inert = state.busy;
        if (listNode) listNode.inert = state.busy;
        if (fieldsNode) fieldsNode.setAttribute('aria-busy', state.busy ? 'true' : 'false');
        updateActionState();
        if (previewNode) {
            previewNode.querySelectorAll('button').forEach(function (item) {
                if (item.dataset.action === 'apply') item.disabled = state.busy || !state.acknowledged || !state.previewToken;
            });
        }
    }

    function toggleMode(record, field) {
        var draft = record.draftFields[field];
        var original = record.state.fields[field] || {};
        if (draft.mode === 'inherit') {
            draft.mode = 'override';
            draft.value = clone(original.effective);
            if (field === 'characteristics') record.characteristicRows = null;
        } else {
            draft.mode = 'inherit';
            draft.value = clone(original.inherited);
            if (field === 'characteristics') record.characteristicRows = null;
        }
        discardPreview();
        renderCurrent();
        showStatus('');
    }

    function resetCurrent() {
        var record = state.byId.get(state.currentId);
        if (!record || !isDirty(record)) return;
        if (!window.confirm('Отменить все несохранённые правки этого товара?')) return;
        var fresh = makeRecord(record.state);
        fresh.recipientKeys = new Set(record.recipientKeys);
        state.byId.set(state.currentId, fresh);
        state.records = state.records.map(function (row) { return row.state.product_id === state.currentId ? fresh : row; });
        discardPreview();
        renderCurrent();
        showStatus('Правки отменены. На странице площадки ничего не менялось.');
    }

    async function refreshCurrent() {
        if (!state.byId.get(state.currentId) || state.busy) return;
        var hasDirty = state.records.some(isDirty);
        if (hasDirty && !window.confirm('Перечитать выбранные товары и отменить все несохранённые правки?')) return;
        clearError();
        setBusy(true);
        try {
            var ids = state.records.map(function (row) { return row.state.product_id; });
            var refreshed = [];
            for (var index = 0; index < ids.length; index += 5) {
                var batch = await Promise.all(ids.slice(index, index + 5).map(function (productId) {
                    return getProduct(productId).then(function (product) { return [productId, product]; });
                }));
                refreshed = refreshed.concat(batch);
            }
            refreshed.forEach(function (pair) {
                var previous = state.byId.get(pair[0]);
                var fresh = makeRecord(pair[1]);
                if (previous) fresh.recipientKeys = new Set(previous.recipientKeys);
                state.byId.set(pair[0], fresh);
            });
            state.records = state.records.map(function (row) { return state.byId.get(row.state.product_id); });
            state.requiresRefresh = false;
            discardPreview();
            renderCurrent();
            showStatus('Данные выбранных товаров перечитаны. Проверьте источник и версии перед новым diff.');
        } catch (error) {
            showError(error && error.message);
        } finally {
            setBusy(false);
        }
    }

    function addCharacteristic(record) {
        var rows = characteristicRows(record);
        if (rows.length >= 100) {
            showError('В одном товаре можно сохранить не больше 100 характеристик.');
            return;
        }
        rows.push({ name: '', value: '', initialValue: '', initialDisplay: '', valueText: '' });
        discardPreview();
        renderCurrent();
        var inputs = fieldsNode.querySelectorAll('[data-char-part="name"]');
        if (inputs.length) inputs[inputs.length - 1].focus();
    }

    function removeCharacteristic(record, index) {
        var rows = characteristicRows(record);
        if (index < 0 || index >= rows.length) return;
        rows.splice(index, 1);
        discardPreview();
        renderCurrent();
    }

    function togglePhoto(record, url) {
        var field = record.draftFields.photos;
        if (field.mode !== 'override') return;
        var option = findPhoto(record, url);
        var photos = photoList(record).slice();
        var index = photos.indexOf(url);
        if (index !== -1) photos.splice(index, 1);
        else {
            if (!option.available_for_selection) {
                showError('Эту фотографию нельзя выбрать: предпросмотр или источник недоступен.');
                return;
            }
            if (photos.length >= 30) {
                showError('Можно выбрать не больше 30 фотографий.');
                return;
            }
            photos.push(url);
        }
        field.value = photos;
        discardPreview();
        renderCurrent();
    }

    function movePhoto(record, url, direction) {
        var field = record.draftFields.photos;
        if (field.mode !== 'override') return;
        var photos = photoList(record).slice();
        var index = photos.indexOf(url);
        var target = index + direction;
        if (index < 0 || target < 0 || target >= photos.length) return;
        photos.splice(index, 1);
        photos.splice(target, 0, url);
        field.value = photos;
        discardPreview();
        renderCurrent();
        var selector = '[data-action="move-photo"][data-photo-url="' + CSS.escape(url) + '"][data-direction="' + direction + '"]';
        var next = fieldsNode.querySelector(selector);
        if (next) next.focus();
    }

    function handleClick(event) {
        if (state.busy) return;
        var target = event.target.closest('[data-action]');
        if (!target) return;
        var action = target.dataset.action;
        if (action === 'choose-product') {
            state.currentId = Number(target.dataset.productId);
            discardPreview();
            renderCurrent();
            showStatus('');
            return;
        }
        if (action === 'toggle-mode') {
            var record = state.byId.get(state.currentId);
            if (record) toggleMode(record, target.dataset.field);
            return;
        }
        if (action === 'toggle-photo') {
            var photoRecord = state.byId.get(state.currentId);
            if (photoRecord) togglePhoto(photoRecord, target.dataset.photoUrl);
            return;
        }
        if (action === 'move-photo') {
            var moveRecord = state.byId.get(state.currentId);
            if (moveRecord) movePhoto(moveRecord, target.dataset.photoUrl, Number(target.dataset.direction));
            return;
        }
        if (action === 'add-characteristic') {
            var addRecord = state.byId.get(state.currentId);
            if (addRecord) addCharacteristic(addRecord);
            return;
        }
        if (action === 'remove-characteristic') {
            var removeRecord = state.byId.get(state.currentId);
            if (removeRecord) removeCharacteristic(removeRecord, Number(target.dataset.charIndex));
            return;
        }
        if (action === 'reset-product') { resetCurrent(); return; }
        if (action === 'refresh-product') { refreshCurrent(); return; }
        if (action === 'preview') { preview(); return; }
        if (action === 'back-to-fields') { discardPreview(); renderCurrent(); return; }
        if (action === 'apply') { applyPreview(); }
    }

    function handleInput(event) {
        if (state.busy) return;
        var record = state.byId.get(state.currentId);
        if (!record) return;
        var target = event.target;
        if (target.dataset.fieldInput) {
            var field = target.dataset.fieldInput;
            record.draftFields[field].value = target.value;
            discardPreview();
            renderProductList();
            updateActionState();
            return;
        }
        if (target.dataset.charIndex !== undefined) {
            var index = Number(target.dataset.charIndex);
            var rows = characteristicRows(record);
            var row = rows[index];
            if (!row) return;
            if (target.dataset.charPart === 'name') row.name = target.value;
            else if (target.dataset.charPart === 'value') {
                row.valueText = target.value;
                row.value = target.value === row.initialDisplay ? clone(row.initialValue) : target.value;
            }
            discardPreview();
            renderProductList();
            updateActionState();
        }
    }

    function handleChange(event) {
        if (state.busy) return;
        var target = event.target;
        if (target.dataset.recipientKey) {
            var record = state.byId.get(state.currentId);
            if (!record) return;
            if (target.checked) record.recipientKeys.add(target.dataset.recipientKey);
            else record.recipientKeys.delete(target.dataset.recipientKey);
            var total = state.records.reduce(function (sum, row) { return sum + selectedRecipients(row).length; }, 0);
            if (total > 100) {
                record.recipientKeys.delete(target.dataset.recipientKey);
                target.checked = false;
                showError('Для сравнения выберите не больше 100 каналов на весь просмотр.');
            } else {
                discardPreview();
                clearError();
                updateActionState();
            }
            return;
        }
        if (target.dataset.action === 'acknowledge-preview') {
            state.acknowledged = target.checked;
            if (state.preview) renderPreview();
        }
    }

    function updateActionState() {
        if (!fieldsNode) return;
        var reviewButton = fieldsNode.querySelector('[data-action="preview"]');
        if (reviewButton) reviewButton.disabled = state.requiresRefresh || !state.records.some(isDirty) || state.busy;
        var reset = fieldsNode.querySelector('[data-action="reset-product"]');
        var current = state.byId.get(state.currentId);
        if (reset) reset.disabled = !current || !isDirty(current) || state.busy;
    }

    if (!Array.isArray(bootstrap.products) || !bootstrap.products.length) return;
    bootstrap.products.forEach(function (product) {
        var record = makeRecord(product);
        state.records.push(record);
        state.byId.set(product.product_id, record);
    });
    state.currentId = bootstrap.products[0].product_id;
    if (fieldsNode) {
        fieldsNode.addEventListener('click', handleClick);
        fieldsNode.addEventListener('input', handleInput);
        fieldsNode.addEventListener('change', handleChange);
    }
    if (previewNode) {
        previewNode.addEventListener('click', handleClick);
        previewNode.addEventListener('change', handleChange);
    }
    if (listNode) listNode.addEventListener('click', handleClick);
    window.addEventListener('beforeunload', function (event) {
        if (!state.busy && !state.records.some(isDirty)) return;
        event.preventDefault();
        event.returnValue = '';
    });
    renderProductList();
    renderCurrent();
})();
