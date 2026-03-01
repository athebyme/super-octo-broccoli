# План качественного роста парсинга товаров от поставщиков

## Текущее состояние (аудит)

### Что работает сейчас
- `SupplierCSVParser` — парсинг CSV для формата sexoptovik (hardcoded) и generic (DictReader)
- `SizeParser` — regex-извлечение физических размеров (длина, диаметр, вес и т.д.)
- `MarketplaceAwareParsingTask` — AI-парсинг характеристик под схему маркетплейса
- `MarketplaceValidator` — валидация данных с fuzzy-matching по словарям (75%)
- `ProductValidator` — базовая валидация (title, vendor_code, photos, barcodes)

### Выявленные слабые места

| # | Проблема | Где в коде | Влияние |
|---|----------|-----------|---------|
| 1 | Hardcoded индексы колонок для sexoptovik (row[0], row[1]...) | `supplier_service.py:134-199` | Любое изменение CSV ломает парсинг |
| 2 | Generic парсер ищет поля по фиксированным названиям | `supplier_service.py:227-276` | Не работает для новых поставщиков с другими заголовками |
| 3 | Нет нормализации данных (пробелы, регистр, unicode) | Весь pipeline | Дубли, ошибки маппинга |
| 4 | Отсутствует валидация CSV до начала парсинга | `supplier_service.py:127-132` | Битые файлы молча ломают импорт |
| 5 | Фото-URL формируются по шаблону без проверки доступности | `supplier_service.py:204-225` | Битые фото на карточках |
| 6 | Описания загружаются отдельным файлом без привязки к основному синку | `supplier_service.py` | Рассинхрон данных |
| 7 | AI-парсинг не кэшируется — повторный запуск = повторный расход токенов | `marketplace_ai_parser.py` | Расходы на AI |
| 8 | Fuzzy-matching с порогом 0.75 без контекста | `marketplace_ai_parser.py:24-60` | Ложные совпадения |
| 9 | Нет отчёта о качестве парсинга по каждому полю | `supplier_service.py:412-527` | Невозможно понять, где теряются данные |
| 10 | SizeParser не обрабатывает многие реальные форматы | `auto_import_manager.py:28-137` | Потеря размеров |

---

## Фаза 1: Надёжность базового парсинга (1-2 недели)

### 1.1 Конфигурируемый маппинг колонок CSV

**Проблема:** Индексы колонок зашиты в код (`row[0]`, `row[1]`...). При смене формата CSV поставщиком — парсинг ломается.

**Решение:**
```python
# Новое поле в модели Supplier
csv_column_mapping = db.Column(db.JSON, default=None)
# Пример значения:
{
    "external_id": {"column": 0, "type": "string"},
    "vendor_code": {"column": 1, "type": "string"},
    "title": {"column": 2, "type": "string"},
    "categories": {"column": 3, "type": "list", "separator": "#"},
    "brand": {"column": 4, "type": "string"},
    "colors": {"column": 9, "type": "list", "separator": ","},
    "photo_codes": {"column": 13, "type": "list", "separator": ","},
    "barcodes": {"column": 14, "type": "list", "separator": "#"},
    "materials": {"column": 15, "type": "list", "separator": ","}
}
```

**Что даст:**
- Добавление нового поставщика без кода — только конфиг в админке
- Устойчивость к изменениям формата CSV
- UI для настройки маппинга (drag & drop колонок на поля)

**Файлы:** `models.py`, `services/supplier_service.py`, `templates/admin_supplier_form.html`

### 1.2 Предпарсинг и валидация CSV

**Проблема:** Парсер начинает обработку не проверяя файл. Битая кодировка, пустые колонки, дубли — обнаруживаются поздно.

**Решение:** Добавить этап pre-validation:

```python
class CSVPreValidator:
    def validate(self, csv_content: str, supplier: Supplier) -> PreValidationResult:
        # 1. Проверить кодировку (chardet)
        # 2. Проверить количество колонок (минимум N для данного маппинга)
        # 3. Детектировать delimiter автоматически (csv.Sniffer)
        # 4. Проверить первые 10 строк на корректность
        # 5. Найти дубли по external_id
        # 6. Показать preview первых 5 товаров
        return PreValidationResult(
            encoding_detected='cp1251',
            delimiter_detected=';',
            total_rows=15234,
            columns_count=16,
            duplicate_ids=3,
            sample_products=[...],
            warnings=[...],
            is_valid=True
        )
```

**Что даст:**
- Ранние ошибки вместо тихого провала
- Авто-детекция кодировки и разделителя
- Preview перед импортом

**Файлы:** новый `services/csv_pre_validator.py`, `routes/suppliers.py`

### 1.3 Нормализация данных

**Проблема:** Данные из CSV приходят грязные — лишние пробелы, разный регистр, unicode-мусор, HTML-сущности.

**Решение:**

```python
class DataNormalizer:
    @staticmethod
    def normalize_title(title: str) -> str:
        # Убрать двойные пробелы, HTML entities, лишние символы
        # Привести первую букву к верхнему регистру
        # Удалить артикул из названия если он дублируется
        pass

    @staticmethod
    def normalize_brand(brand: str) -> str:
        # Привести к каноническому написанию из словаря
        # "LELO" == "Lelo" == "lelo"
        pass

    @staticmethod
    def normalize_color(color: str) -> str:
        # Маппинг вариаций: "чёрный" == "черный" == "Чёрный"
        # Маппинг к цветам WB из словаря
        pass

    @staticmethod
    def normalize_barcode(barcode: str) -> Optional[str]:
        # Проверить формат EAN-13/EAN-8
        # Убрать пробелы и нечисловые символы
        # Валидировать контрольную цифру
        pass
```

**Что даст:**
- Чистые данные с первого этапа
- Меньше ошибок при AI-парсинге (чище вход → чище выход)
- Корректные баркоды

**Файлы:** новый `services/data_normalizer.py`, интеграция в `supplier_service.py`

---

## Фаза 2: Интеллектуальный маппинг категорий (2-3 недели)

### 2.1 Многоуровневый маппинг категорий

**Проблема:** `CategoryMapping` хранит простое соответствие `source_category → wb_subject_id`. Не учитывает контекст товара — название, бренд, другие поля.

**Решение:**

```python
class SmartCategoryMapper:
    def map_category(self, product_data: dict) -> CategoryMappingResult:
        # Уровень 1: Точное совпадение по категории поставщика
        result = self._exact_category_match(product_data['category'])
        if result and result.confidence >= 0.95:
            return result

        # Уровень 2: Маппинг по категории + бренд + ключевые слова из title
        result = self._contextual_match(product_data)
        if result and result.confidence >= 0.85:
            return result

        # Уровень 3: AI-маппинг с подтверждением
        result = self._ai_category_match(product_data)
        if result:
            result.needs_review = result.confidence < 0.9
            return result

        return CategoryMappingResult(confidence=0, needs_review=True)
```

**Что даст:**
- Точность маппинга с текущих ~70% до 90%+
- Учёт контекста товара, а не только названия категории
- Автоматическое обучение на ручных коррекциях

### 2.2 Обратная связь от ручных коррекций

**Проблема:** `ProductCategoryCorrection` хранит коррекции, но они не влияют на будущий маппинг.

**Решение:**
- При ручной коррекции — автоматически создавать/обновлять `CategoryMapping`
- Собирать статистику: какие категории чаще корректируются → показывать в дашборде
- Periodic job: пересчитать маппинг для `draft` товаров на основе новых коррекций

**Файлы:** `services/supplier_service.py`, `routes/suppliers.py`, `models.py`

---

## Фаза 3: Улучшение AI-парсинга (2-3 недели)

### 3.1 Кэширование AI-результатов

**Проблема:** Каждый запуск AI-парсинга — новый API вызов даже если данные товара не менялись.

**Решение:**

```python
class AIParsingCache:
    def get_or_parse(self, product: SupplierProduct, task: MarketplaceAwareParsingTask) -> Dict:
        # Хэш от входных данных товара (title + description + category + ...)
        input_hash = self._compute_hash(product)

        # Проверяем кэш
        if product.ai_input_hash == input_hash and product.ai_parsed_data_json:
            return json.loads(product.ai_parsed_data_json)

        # Парсим и сохраняем
        result = task.execute(...)
        product.ai_input_hash = input_hash
        product.ai_parsed_data_json = json.dumps(result)
        return result
```

**Что даст:**
- Экономия 50-80% расходов на AI при повторных синхронизациях
- Быстрее ре-синк (пропуск неизменённых товаров)

### 3.2 Двухпроходный AI-парсинг с retry

**Проблема:** Сейчас есть задатки двухпроходности (missing_required в `parse_response`), но второй проход не реализован.

**Решение:**

```python
class TwoPassAIParsing:
    def parse(self, product_data: dict, characteristics: list) -> dict:
        # Проход 1: Полный парсинг всех характеристик
        result = self._full_parse(product_data, characteristics)

        # Проход 2: Точечный re-query для пропущенных обязательных полей
        missing = result['_meta']['missing_required']
        if missing:
            missing_charcs = [c for c in characteristics if c.name in missing]
            retry_result = self._targeted_parse(product_data, missing_charcs)
            result = self._merge_results(result, retry_result)

        # Проход 3 (опционально): Перекрёстная валидация
        result = self._cross_validate(result, product_data)

        return result
```

**Что даст:**
- Заполнение обязательных полей с ~75% до ~90%
- Retry только для пропущенных полей — не тратим токены на всё заново

### 3.3 Улучшение промптов

**Проблема:** Системный промпт в `MarketplaceAwareParsingTask.get_system_prompt()` общий. Не учитывает специфику категорий.

**Решение:**
- Добавить few-shot examples в промпт из уже валидированных товаров той же категории
- Использовать `ai_example_value` и `ai_instruction` из характеристик более агрессивно
- Для словарных полей с >25 значениями — передавать только релевантные (по TF-IDF сходству с описанием товара)

```python
def get_system_prompt(self) -> str:
    # ... существующий код ...

    # Добавляем примеры из уже успешно валидированных товаров
    examples = self._get_validated_examples(category_id, limit=3)
    if examples:
        sys_prompt += "\n\nПРИМЕРЫ ПРАВИЛЬНОГО ЗАПОЛНЕНИЯ:\n"
        for ex in examples:
            sys_prompt += f"Input: {ex['title']}\nOutput: {json.dumps(ex['parsed'], ensure_ascii=False)}\n\n"
```

**Файлы:** `services/marketplace_ai_parser.py`, `services/ai_service.py`

### 3.4 Конфиденс-скоринг и приоритизация ручной проверки

**Проблема:** Все товары после AI-парсинга равны — нет понимания, какие требуют проверки.

**Решение:**

```python
class ParsingConfidenceScorer:
    def score(self, product: SupplierProduct, parsed_data: dict) -> float:
        score = 1.0

        # -0.2 за каждое обязательное поле из fuzzy match (а не exact)
        # -0.3 за каждое пропущенное обязательное поле
        # -0.1 за каждое поле с validation issue
        # -0.15 если категория с низким confidence
        # +0.1 если есть аналогичный товар с подтверждёнными данными

        return max(0, min(1, score))
```

**Что даст:**
- Автоматическая очередь ручной проверки — сначала самые проблемные
- Дашборд "Здоровья парсинга" — % товаров по уровням confidence

---

## Фаза 4: Мониторинг и аналитика качества (1-2 недели)

### 4.1 Дашборд качества парсинга

Новая страница в админке `/admin/parsing-quality`:

```
┌─────────────────────────────────────────────────────┐
│  Качество парсинга                                   │
│                                                       │
│  Поставщик: [Sexoptovik ▼]     Период: [7 дней ▼]   │
│                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐│
│  │  Всего: 15234│  │Заполнено >90%│  │ Ошибки: 234  ││
│  │              │  │    12,456    │  │              ││
│  └──────────────┘  └──────────────┘  └──────────────┘│
│                                                       │
│  Проблемные поля (топ-10):                           │
│  ┌────────────────────────┬──────────┬──────────────┐│
│  │ Поле                   │Заполнено │ Fuzzy-fix    ││
│  ├────────────────────────┼──────────┼──────────────┤│
│  │ Материал               │    67%   │     23%      ││
│  │ Страна производства    │    45%   │     12%      ││
│  │ Состав                 │    38%   │      8%      ││
│  └────────────────────────┴──────────┴──────────────┘│
│                                                       │
│  Тренд заполняемости (по неделям): [график]          │
└─────────────────────────────────────────────────────┘
```

### 4.2 Детальное логирование парсинга

**Проблема:** Логи пишутся в один файл, нет structured logging.

**Решение:**

```python
class ParsingMetrics:
    def log_sync(self, supplier_id, metrics: dict):
        # Сохраняем в БД для аналитики
        ParsingLog.create(
            supplier_id=supplier_id,
            event_type='sync',
            total_products=metrics['total'],
            parsed_successfully=metrics['success'],
            field_fill_rates=metrics['field_rates'],  # JSON: {"title": 0.99, "brand": 0.87, ...}
            ai_tokens_used=metrics['tokens'],
            duration_seconds=metrics['duration'],
            errors_json=metrics['errors']
        )
```

**Новая модель:**
```python
class ParsingLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))
    event_type = db.Column(db.String(50))  # sync, ai_parse, validate, import
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    total_products = db.Column(db.Integer)
    parsed_successfully = db.Column(db.Integer)
    field_fill_rates = db.Column(db.JSON)
    ai_tokens_used = db.Column(db.Integer)
    duration_seconds = db.Column(db.Float)
    errors_json = db.Column(db.JSON)
```

---

## Фаза 5: Расширение форматов и источников (2-4 недели)

### 5.1 Поддержка XML/JSON фидов

**Проблема:** Только CSV. Многие поставщики отдают XML (YML/Яндекс.Маркет) или JSON API.

**Решение:**

```python
class SupplierFeedParser:
    """Единый интерфейс для разных форматов"""

    @staticmethod
    def create(supplier: Supplier) -> 'BaseFeedParser':
        feed_type = supplier.feed_type  # csv, xml_yml, json_api
        if feed_type == 'csv':
            return CSVFeedParser(supplier)
        elif feed_type == 'xml_yml':
            return YMLFeedParser(supplier)
        elif feed_type == 'json_api':
            return JSONAPIFeedParser(supplier)
        raise ValueError(f"Unknown feed type: {feed_type}")

class YMLFeedParser(BaseFeedParser):
    """Парсер YML (Yandex Market Language) — самый популярный формат в РФ"""
    def parse(self, content: str) -> List[Dict]:
        # Стандартная структура: <offers><offer id="...">
        # Поля: name, vendor, description, picture, barcode, param
        pass

class JSONAPIFeedParser(BaseFeedParser):
    """Парсер JSON API с пагинацией"""
    def parse(self, content: str) -> List[Dict]:
        pass
```

### 5.2 Проверка фото-URL

**Проблема:** Фото-URL формируются по шаблону, но могут быть недоступны (404, тайм-аут).

**Решение:**
- При синхронизации делать HEAD-запрос к первому фото каждого товара (batch, async)
- Отмечать товары с битыми фото флагом `photos_verified = False`
- Фоновый job: проверять все фото раз в сутки

### 5.3 Автоматическое обогащение из описания

**Проблема:** Описание товара загружается отдельно и не используется для извлечения характеристик.

**Решение:**
- После загрузки описаний — запускать regex-извлечение параметров
- Дополнять данные товара: размеры, материалы, страна из текста описания
- Использовать описание как дополнительный контекст для AI-парсинга

---

## Фаза 6: UX админки для управления качеством (1-2 недели)

### 6.1 Визуальный preview перед синхронизацией

```
[Синхронизировать каталог]
        ↓
┌─────────────────────────────────────────────┐
│  Preview синхронизации                       │
│                                               │
│  Файл: sexoptovik_catalog.csv                │
│  Строк: 15,234  |  Кодировка: cp1251        │
│  Дубликатов: 3   |  Пустых строк: 12        │
│                                               │
│  Первые 5 товаров:                           │
│  ┌─────────┬────────────┬─────────┬────────┐ │
│  │ ID      │ Название   │ Бренд   │ Фото   │ │
│  ├─────────┼────────────┼─────────┼────────┤ │
│  │ id-1234 │ Вибратор...│ LELO    │ 4 шт   │ │
│  │ ...     │ ...        │ ...     │ ...    │ │
│  └─────────┴────────────┴─────────┴────────┘ │
│                                               │
│  Изменения: +234 новых / ~89 обновлений      │
│                                               │
│  [Отмена]                [Подтвердить импорт] │
└─────────────────────────────────────────────┘
```

### 6.2 Batch-редактор проблемных товаров

Страница с фильтром по проблемам:
- `confidence < 0.7` — товары с низким доверием
- `missing_required` — не заполнены обязательные поля
- `fuzzy_matched` — значения были исправлены fuzzy-matching

Возможности:
- Массовая коррекция категорий
- Массовое назначение бренда
- Быстрый AI re-parse выбранных товаров
- Экспорт проблемных товаров в Excel для ручной обработки

### 6.3 Правила автокоррекции

Настраиваемые правила в админке:
```
Если title содержит "LELO" и brand пустой → brand = "LELO"
Если category = "Кольца" и gender пустой → gender = "мужской"
Если country пустой и brand в словаре → country = словарь[brand]
```

---

## Приоритизация и план внедрения

| Приоритет | Задача | Эффект | Сложность |
|-----------|--------|--------|-----------|
| **P0** | 1.3 Нормализация данных | Высокий — чистые данные на входе | Средняя |
| **P0** | 3.1 Кэширование AI | Высокий — экономия 50%+ расходов | Низкая |
| **P1** | 1.1 Конфигурируемый маппинг колонок | Высокий — масштабируемость | Средняя |
| **P1** | 1.2 Предпарсинг и валидация | Средний — ранние ошибки | Средняя |
| **P1** | 3.2 Двухпроходный AI-парсинг | Высокий — больше заполненных полей | Средняя |
| **P1** | 3.3 Улучшение промптов | Высокий — качество AI | Средняя |
| **P2** | 2.1 Многоуровневый маппинг категорий | Высокий — точность категорий | Высокая |
| **P2** | 3.4 Конфиденс-скоринг | Средний — приоритизация работы | Низкая |
| **P2** | 4.1 Дашборд качества | Средний — видимость проблем | Средняя |
| **P2** | 4.2 Structured logging | Средний — отладка | Низкая |
| **P3** | 5.1 XML/JSON фиды | Средний — новые поставщики | Высокая |
| **P3** | 5.2 Проверка фото | Низкий — качество карточек | Низкая |
| **P3** | 6.1-6.3 UX улучшения | Средний — удобство | Средняя |

## Метрики успеха

| Метрика | Текущее (оценка) | Цель |
|---------|-----------------|------|
| Заполненность обязательных полей | ~75% | >95% |
| Точность маппинга категорий | ~70% | >90% |
| Доля товаров без ручной правки | ~50% | >80% |
| Время парсинга 10K товаров | ~15 мин | <10 мин |
| Расход AI-токенов на ре-синк | 100% (каждый раз) | ~30% (только изменённые) |
| Доля битых фото | Неизвестно | <1% |
