# TODO: Завершение функционала объединения карточек

## ✅ Что уже сделано

1. **Модель БД** (`models.py`):
   - Класс `CardMergeHistory` для истории объединений
   - Поддержка операций merge/unmerge
   - Откат операций
   - Снимки состояния до/после

2. **Миграция БД** (`migrate_add_card_merge_history.py`):
   - Создание таблицы `card_merge_history`
   - Индексы для быстрого поиска
   - Добавлена в `docker-entrypoint.sh`

3. **API клиент** (`wb_api_client.py`):
   - `merge_cards()` - объединение карточек
   - `unmerge_cards()` - разъединение карточек
   - Валидация и обработка ошибок
   - Логирование

4. **Роуты Flask** (`routes_merge_cards.py`):
   - `/products/merge` - страница выбора карточек
   - `/products/merge/execute` - выполнение объединения
   - `/products/merge/history` - список истории
   - `/products/merge/history/<id>` - детали операции
   - `/products/merge/revert/<id>` - откат объединения

## 🔄 Что нужно доделать

### 1. Интеграция роутов в seller_platform.py

Добавить в конец файла `seller_platform.py` перед `if __name__ == '__main__':`:

```python
# Подключаем роуты для объединения карточек
from routes_merge_cards import register_merge_routes
register_merge_routes(app)
```

### 2. Создать HTML templates

#### `templates/products_merge.html`

Должен содержать:
- Фильтр по категориям (subject_id)
- Группировку карточек по imtID (показать уже объединенные)
- Возможность выбрать главную карточку (radio button)
- Множественный выбор карточек для объединения (checkboxes)
- Валидацию на фронтенде (одинаковый subject_id)
- Кнопку "Объединить карточки"
- Показ количества выбранных карточек

Структура:
```html
{% extends "base.html" %}
{% block content %}
<div class="container">
    <h2>Объединение карточек товаров</h2>

    <!-- Фильтр по категориям -->
    <div class="mb-3">
        <label>Категория (subject_id):</label>
        <select class="form-control" onchange="filterBySubject(this.value)">
            <option value="">Все категории</option>
            {% for subject in subjects %}
            <option value="{{ subject.id }}">{{ subject.name }}</option>
            {% endfor %}
        </select>
    </div>

    <!-- Группы карточек -->
    <form method="POST" action="{{ url_for('products_merge_execute') }}">
        {% for group in imt_groups %}
        <div class="card mb-3">
            <div class="card-header">
                <strong>{{ group.subject_name }}</strong>
                {% if group.imt_id %}
                <span class="badge badge-info">imtID: {{ group.imt_id }}</span>
                {% else %}
                <span class="badge badge-secondary">Не объединены</span>
                {% endif %}
            </div>
            <div class="card-body">
                {% for card in group.cards %}
                <div class="form-check">
                    <input type="radio" name="target_nm_id" value="{{ card.nm_id }}"
                           class="form-check-input target-radio" data-subject="{{ card.subject_id }}">
                    <input type="checkbox" name="merge_nm_ids" value="{{ card.nm_id }}"
                           class="form-check-input merge-checkbox" data-subject="{{ card.subject_id }}">
                    <label class="form-check-label">
                        <strong>{{ card.vendor_code }}</strong> - {{ card.title }}
                        <small class="text-muted">(nmID: {{ card.nm_id }})</small>
                    </label>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endfor %}

        <input type="hidden" name="nm_ids" id="nm_ids_input">

        <button type="submit" class="btn btn-primary" onclick="return validateMerge()">
            Объединить карточки
        </button>
    </form>
</div>

<script>
function validateMerge() {
    const target = document.querySelector('input[name="target_nm_id"]:checked');
    const checks = document.querySelectorAll('input[name="merge_nm_ids"]:checked');

    if (!target) {
        alert('Выберите главную карточку (radio button)');
        return false;
    }

    if (checks.length === 0) {
        alert('Выберите хотя бы одну карточку для объединения');
        return false;
    }

    // Собираем nmIDs
    const nmIds = Array.from(checks).map(c => c.value);
    document.getElementById('nm_ids_input').value = nmIds.join(',');

    return confirm(`Объединить ${checks.length} карточек к imtID главной карточки?`);
}
</script>
{% endblock %}
```

#### `templates/products_merge_history.html`

Список истории объединений с кнопками отката

#### `templates/products_merge_detail.html`

Детали конкретного объединения с показом изменений

### 3. Добавить пункт меню в навигацию

В `templates/base.html` или аналогичный файл добавить:

```html
<li class="nav-item">
    <a class="nav-link" href="{{ url_for('products_merge') }}">
        Объединение карточек
    </a>
</li>
```

### 4. Тестирование

1. Пересобрать контейнер:
   ```bash
   docker-compose build seller-platform
   docker-compose restart seller-platform
   ```

2. Проверить миграцию:
   ```bash
   docker-compose logs seller-platform | grep "card_merge_history"
   ```

3. Протестировать:
   - Выбор карточек и объединение
   - Проверка что imtID обновился в БД
   - Просмотр истории
   - Откат объединения

## 📋 Ключевые особенности реализации

- **Валидация**: Можно объединять только карточки с одинаковым `subject_id`
- **Лимиты**: Максимум 30 карточек за раз (API WB)
- **История**: Все операции логируются с снимками состояния
- **Откат**: Возможность отката с автоматическим разъединением
- **UI**: Группировка по imtID для показа уже объединенных карточек

## 🔗 Документация WB API

- Endpoint: `POST /content/v2/cards/moveNm`
- Для объединения: `{"targetIMT": 123, "nmIDs": [111, 222]}`
- Для разъединения: `{"nmIDs": [111, 222]}`
- Ограничения: макс 30 карточек, только одинаковые subject_id
