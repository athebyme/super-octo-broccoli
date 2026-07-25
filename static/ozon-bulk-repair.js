(function () {
    'use strict';

    function init() {
        const root = document.querySelector('[data-ozon-repair-root]');
        if (!root) return;

        const form = root.querySelector('[data-repair-form]');
        const allRows = () => Array.from(
            root.querySelectorAll('[data-repair-row]')
        );
        const selectedRows = (scope) => Array.from(
            (scope || root).querySelectorAll('[data-repair-row]')
        ).filter((row) => row.querySelector('[data-row-select]')?.checked);
        const fieldTarget = (row, key) => Array.from(
            row.querySelectorAll('[data-field-key]')
        ).find((input) => input.dataset.fieldKey === key);

        function updateSelection() {
            const selected = selectedRows().length;
            const total = allRows().length;
            root.querySelectorAll('[data-selection-count]').forEach((node) => {
                node.textContent = `Выбрано ${selected} из ${total}`;
            });
            const submitLabel = root.querySelector('[data-submit-selection]');
            if (submitLabel) {
                submitLabel.textContent = selected
                    ? `Будет проверено карточек: ${selected}`
                    : 'Карточки не выбраны';
            }
            const submit = root.querySelector('[data-repair-submit]');
            if (submit) submit.disabled = selected === 0;
        }

        root.addEventListener('change', (event) => {
            if (event.target.matches('[data-row-select]')) {
                updateSelection();
            }
        });
        root.querySelector('[data-select-all]')?.addEventListener('click', () => {
            allRows().forEach((row) => {
                const checkbox = row.querySelector('[data-row-select]');
                if (checkbox) checkbox.checked = true;
            });
            updateSelection();
        });
        root.querySelector('[data-select-none]')?.addEventListener('click', () => {
            allRows().forEach((row) => {
                const checkbox = row.querySelector('[data-row-select]');
                if (checkbox) checkbox.checked = false;
            });
            updateSelection();
        });
        root.querySelectorAll('[data-group-toggle]').forEach((button) => {
            button.addEventListener('click', () => {
                const group = button.closest('[data-repair-group]');
                const rows = allRows().filter((row) => group.contains(row));
                const shouldSelect = rows.some(
                    (row) => !row.querySelector('[data-row-select]')?.checked
                );
                rows.forEach((row) => {
                    const checkbox = row.querySelector('[data-row-select]');
                    if (checkbox) checkbox.checked = shouldSelect;
                });
                updateSelection();
            });
        });

        root.querySelector('[data-bulk-apply]')?.addEventListener('click', () => {
            const key = root.querySelector('[data-bulk-field]')?.value || '';
            const inputValue = root.querySelector(
                '[data-bulk-value]'
            )?.value ?? '';
            let value = inputValue;
            let copiedFromRow = false;
            if (!value.trim()) {
                for (const row of selectedRows()) {
                    const source = fieldTarget(row, key);
                    if (!source || source.disabled) continue;
                    if (source.type === 'checkbox') {
                        value = source.checked ? 'да' : 'нет';
                        copiedFromRow = true;
                        break;
                    }
                    if (String(source.value || '').trim()) {
                        value = source.value;
                        copiedFromRow = true;
                        break;
                    }
                }
                if (!copiedFromRow) {
                    const message = root.querySelector(
                        '[data-bulk-message]'
                    );
                    if (message) {
                        message.textContent =
                            'Введите значение или сначала заполните его в одной выбранной карточке';
                    }
                    return;
                }
            }
            const normalized = value.trim().toLocaleLowerCase('ru-RU');
            const trueValues = new Set(['1', 'true', 'да', 'yes']);
            const falseValues = new Set(['0', 'false', 'нет', 'no']);
            if (
                key === 'schema_cleanup'
                && !trueValues.has(normalized)
                && !falseValues.has(normalized)
            ) {
                const message = root.querySelector('[data-bulk-message]');
                if (message) {
                    message.textContent =
                        'Для очистки укажите «да» или «нет»';
                }
                return;
            }
            let changed = 0;
            selectedRows().forEach((row) => {
                const target = fieldTarget(row, key);
                if (!target || target.disabled) return;
                if (target.type === 'checkbox') {
                    target.checked = trueValues.has(normalized);
                } else if (
                    target.tagName === 'SELECT'
                    && target.querySelector('option[value="true"]')
                ) {
                    target.value = trueValues.has(normalized)
                        ? 'true'
                        : falseValues.has(normalized) ? 'false' : value;
                } else {
                    target.value = value;
                }
                target.dispatchEvent(new Event('input', {bubbles: true}));
                target.dispatchEvent(new Event('change', {bubbles: true}));
                changed += 1;
            });
            const message = root.querySelector('[data-bulk-message]');
            if (message) {
                message.textContent = changed
                    ? (
                        copiedFromRow
                            ? `Значение из заполненной строки скопировано для ${changed} карточек`
                            : `Поле заполнено в ${changed} строках`
                    )
                    : 'В выбранных строках этого поля нет';
            }
        });

        const typeSearchUrl = root.dataset.typeSearchUrl || '';
        root.querySelectorAll('[data-type-picker]').forEach((picker) => {
            const group = picker.closest('[data-repair-group]');
            const input = picker.querySelector('[data-type-search]');
            const results = picker.querySelector('[data-type-results]');
            const mapping = picker.querySelector('[data-group-save-mapping]');
            const selectedLabel = picker.querySelector('[data-selected-type]');
            let timer = null;
            let requestNumber = 0;

            function syncMappingFlag() {
                group.querySelectorAll('[data-save-mapping]').forEach((hidden) => {
                    hidden.value = mapping?.checked ? '1' : '0';
                });
            }
            mapping?.addEventListener('change', syncMappingFlag);

            function choose(item) {
                group.querySelectorAll('[data-product-type-id]').forEach(
                    (hidden) => { hidden.value = String(item.id); }
                );
                if (mapping) mapping.checked = true;
                syncMappingFlag();
                if (selectedLabel) {
                    selectedLabel.textContent =
                        `${item.name} · ${item.category_path || ''}`;
                }
                input.value = item.name;
                results.hidden = true;
                results.replaceChildren();
                input.setAttribute('aria-expanded', 'false');
            }

            async function search() {
                const query = input.value.trim();
                if (!query) {
                    results.hidden = true;
                    input.setAttribute('aria-expanded', 'false');
                    return;
                }
                const currentRequest = ++requestNumber;
                try {
                    const response = await fetch(
                        `${typeSearchUrl}?q=${encodeURIComponent(query)}`,
                        {headers: {'Accept': 'application/json'}}
                    );
                    const payload = await response.json();
                    if (currentRequest !== requestNumber) return;
                    results.replaceChildren();
                    if (!response.ok || !payload.success) {
                        throw new Error(payload.error || 'Поиск недоступен');
                    }
                    payload.items.forEach((item) => {
                        const button = document.createElement('button');
                        button.type = 'button';
                        button.className = 'ozr-result';
                        button.textContent = item.name;
                        const path = document.createElement('small');
                        path.textContent = item.category_path || '';
                        button.appendChild(path);
                        button.addEventListener('click', () => choose(item));
                        results.appendChild(button);
                    });
                    if (!payload.items.length) {
                        const empty = document.createElement('div');
                        empty.className = 'ozr-result';
                        empty.textContent = 'Ничего не найдено';
                        results.appendChild(empty);
                    }
                    results.hidden = false;
                    input.setAttribute('aria-expanded', 'true');
                } catch (error) {
                    results.replaceChildren();
                    const message = document.createElement('div');
                    message.className = 'ozr-result';
                    message.textContent = error.message || 'Поиск недоступен';
                    results.appendChild(message);
                    results.hidden = false;
                    input.setAttribute('aria-expanded', 'true');
                }
            }

            input?.addEventListener('input', () => {
                window.clearTimeout(timer);
                timer = window.setTimeout(search, 250);
            });
            input?.addEventListener('keydown', (event) => {
                if (event.key === 'Escape') {
                    results.hidden = true;
                    input.setAttribute('aria-expanded', 'false');
                } else if (event.key === 'ArrowDown') {
                    const first = results.querySelector('button');
                    if (first) {
                        event.preventDefault();
                        first.focus();
                    }
                }
            });
        });

        root.querySelectorAll('[data-dictionary-input]').forEach((input) => {
            const wrapper = input.closest('.ozr-search-wrap');
            const results = wrapper.querySelector('[data-dictionary-results]');
            const valueId = wrapper.querySelector('[data-dictionary-value-id]');
            let timer = null;
            let requestNumber = 0;
            let choosing = false;

            function choose(item) {
                choosing = true;
                input.value = item.value;
                valueId.value = item.id;
                results.hidden = true;
                results.replaceChildren();
                input.setAttribute('aria-expanded', 'false');
                input.dispatchEvent(new Event('change', {bubbles: true}));
                choosing = false;
            }

            async function search() {
                const currentRequest = ++requestNumber;
                try {
                    const query = input.value.trim();
                    const separator = input.dataset.dictionaryUrl.includes('?')
                        ? '&' : '?';
                    const response = await fetch(
                        `${input.dataset.dictionaryUrl}${separator}q=${encodeURIComponent(query)}`,
                        {headers: {'Accept': 'application/json'}}
                    );
                    const payload = await response.json();
                    if (currentRequest !== requestNumber) return;
                    results.replaceChildren();
                    if (!response.ok || !payload.success) {
                        throw new Error(payload.error || 'Справочник недоступен');
                    }
                    payload.items.forEach((item) => {
                        const button = document.createElement('button');
                        button.type = 'button';
                        button.className = 'ozr-result';
                        button.textContent = item.value;
                        const id = document.createElement('small');
                        id.textContent = `ID ${item.id}`;
                        button.appendChild(id);
                        button.addEventListener('click', () => choose(item));
                        results.appendChild(button);
                    });
                    if (!payload.items.length) {
                        const empty = document.createElement('div');
                        empty.className = 'ozr-result';
                        empty.textContent = 'Точного значения не найдено';
                        results.appendChild(empty);
                    }
                    results.hidden = false;
                    input.setAttribute('aria-expanded', 'true');
                } catch (error) {
                    results.replaceChildren();
                    const message = document.createElement('div');
                    message.className = 'ozr-result';
                    message.textContent = error.message || 'Справочник недоступен';
                    results.appendChild(message);
                    results.hidden = false;
                    input.setAttribute('aria-expanded', 'true');
                }
            }

            input.addEventListener('input', () => {
                if (!choosing) valueId.value = '';
                window.clearTimeout(timer);
                timer = window.setTimeout(search, 250);
            });
            input.addEventListener('focus', () => {
                window.clearTimeout(timer);
                timer = window.setTimeout(search, 100);
            });
            input.addEventListener('keydown', (event) => {
                if (event.key === 'Escape') {
                    results.hidden = true;
                    input.setAttribute('aria-expanded', 'false');
                } else if (event.key === 'ArrowDown') {
                    const first = results.querySelector('button');
                    if (first) {
                        event.preventDefault();
                        first.focus();
                    }
                }
            });
        });

        root.querySelectorAll('[data-attribute-suggestion]').forEach((button) => {
            button.addEventListener('click', () => {
                const field = button.closest('[data-attribute-field]');
                const input = field?.querySelector('[data-dictionary-input]');
                const valueId = field?.querySelector(
                    '[data-dictionary-value-id]'
                );
                if (!input || !valueId || input.disabled) return;
                input.value = button.dataset.suggestionValue || '';
                valueId.value = button.dataset.suggestionId || '';
                field.querySelectorAll('[data-attribute-suggestion]').forEach(
                    (candidate) => {
                        candidate.setAttribute(
                            'aria-pressed',
                            candidate === button ? 'true' : 'false'
                        );
                    }
                );
                const results = field.querySelector(
                    '[data-dictionary-results]'
                );
                if (results) {
                    results.hidden = true;
                    results.replaceChildren();
                }
                input.setAttribute('aria-expanded', 'false');
                input.dispatchEvent(new Event('change', {bubbles: true}));
                input.focus();
            });
        });

        document.addEventListener('click', (event) => {
            root.querySelectorAll('.ozr-results:not([hidden])').forEach((list) => {
                if (!list.parentElement.contains(event.target)) {
                    list.hidden = true;
                    const input = list.parentElement.querySelector(
                        '[role="combobox"]'
                    );
                    input?.setAttribute('aria-expanded', 'false');
                }
            });
        });

        form?.addEventListener('submit', () => {
            root.querySelectorAll('[data-repair-group]').forEach((group) => {
                const checked = group.querySelector(
                    '[data-group-save-mapping]'
                )?.checked;
                group.querySelectorAll('[data-save-mapping]').forEach(
                    (hidden) => { hidden.value = checked ? '1' : '0'; }
                );
            });
            const submit = root.querySelector('[data-repair-submit]');
            if (submit) {
                submit.disabled = true;
                submit.classList.add('is-loading');
                submit.textContent = 'Сохраняем и проверяем';
            }
        });

        updateSelection();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
