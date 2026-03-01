# Качество парсинга товаров

Система улучшения качества парсинга товаров от поставщиков. Включает нормализацию данных, автокоррекцию, умный маппинг категорий, обогащение из описаний и мониторинг качества.

---

## Полный pipeline обработки

Данные товара проходят через цепочку обработчиков:

```
CSV файл → PreValidator → Parser → Normalizer → Enricher → AutoCorrection → DB
                                                                              ↓
                                                            SmartCategoryMapper + ConfidenceScorer
```

### Шаги pipeline

| Шаг | Сервис | Что делает |
|-----|--------|------------|
| 1 | `CSVPreValidator` | Проверка CSV до парсинга: кодировка, разделитель, дубли |
| 2 | `SupplierCSVParser.parse()` | Парсинг CSV в список dict |
| 3 | `DataNormalizer` | Очистка и нормализация: title, brand, color, barcode |
| 4 | `DescriptionEnricher` | Извлечение характеристик из текста описания |
| 5 | `AutoCorrectionEngine` | Автокоррекция по правилам (бренд, страна, пол, мусор) |
| 6a | `SmartCategoryMapper` | Маппинг категории поставщика в WB subject_id |
| 6b | `ParsingConfidenceScorer` | Расчёт оценки качества 0.0-1.0 |
| 6c | `AIParsingCache` | Кэширование AI-результатов по хэшу данных |

### Вызов в коде

```python
# services/supplier_service.py
def parse_and_normalize(self, csv_content: str) -> List[Dict]:
    products = self.parse(csv_content)                           # Шаг 2
    normalized = DataNormalizer.normalize_product_list(products)  # Шаг 3
    normalized = DescriptionEnricher.enrich_product_list(normalized)  # Шаг 4
    engine = get_default_engine()
    normalized = engine.apply_to_list(normalized)                # Шаг 5
    return normalized
```

SmartCategoryMapper и ParsingConfidenceScorer вызываются при сохранении в `_update_supplier_product()`.

---

## Сервисы

### CSVPreValidator

**Файл:** `services/csv_pre_validator.py`

Быстрая проверка CSV файла до полного парсинга.

- Автодетекция кодировки (cp1251, utf-8 и др.)
- Автодетекция разделителя (csv.Sniffer + fallback)
- Подсчёт строк, колонок, пустых строк
- Поиск дублей по external_id
- Preview первых N товаров
- Генерация списка warnings

```python
result = CSVPreValidator.validate(csv_content, expected_columns=16)
# result.is_valid, result.encoding_detected, result.warnings
```

---

### DataNormalizer

**Файл:** `services/data_normalizer.py`

Нормализация и очистка данных. Работает до AI - чистый вход дает лучший результат.

- **Title**: двойные пробелы, HTML-entities, Unicode-мусор, капитализация
- **Brand**: каноническое написание по словарю (~30 брендов): `lelo` -> `LELO`
- **Color**: нормализация русских цветов + маппинг к палитре WB
- **Barcode**: валидация EAN-13/EAN-8 с проверкой контрольной суммы
- **Materials**: нормализация каждого материала, удаление мусорных

```python
products = DataNormalizer.normalize_product_list(raw_products)
```

---

### DescriptionEnricher

**Файл:** `services/description_enricher.py`

Извлечение характеристик из текстового описания товара (regex).

**Что извлекает:**
- Размеры: длина, диаметр, ширина, высота, рабочая длина, вес (с конвертацией см -> мм)
- Материалы: паттерны `материал: ...`, `изготовлен из ...`
- Цвета: паттерн `цвет: ...`
- Страна: `страна: ...`, `made in ...` + словарь стран
- Тип питания: батарейки, USB, аккумулятор

**Принцип:** заполняет только пустые поля, не перезаписывает существующие данные.

```python
enriched = DescriptionEnricher.enrich_from_description(product_data)
enriched_list = DescriptionEnricher.enrich_product_list(products)
```

---

### AutoCorrectionEngine

**Файл:** `services/auto_correction_rules.py`

Конфигурируемый движок автокоррекции с приоритетными правилами.

**Встроенные правила:**

| Приоритет | Имя | Что делает |
|-----------|-----|------------|
| 100 | `brand_from_title` | Извлечь бренд из title по словарю (~27 брендов) |
| 90 | `country_from_brand` | Определить страну по бренду (~24 маппинга) |
| 80 | `gender_from_category` | Определить пол по категории |
| 70 | `clean_title_vendor_code` | Убрать артикул поставщика из начала title |
| 60 | `clean_garbage_values` | Удалить мусорные значения: `-`, `n/a`, `нет данных`, `null` |

Можно добавлять свои правила:

```python
engine = get_default_engine()
engine.add_rule(CorrectionRule(
    name='my_rule', priority=50,
    condition=lambda d: ...,
    action=lambda d: ...,
))
```

---

### SmartCategoryMapper

**Файл:** `services/smart_category_mapper.py`

Многоуровневый маппинг категорий поставщика в WB subject_id.

**Уровни (по приоритету):**

| # | Уровень | Confidence |
|---|---------|------------|
| 1 | Ручные исправления (ProductCategoryCorrection) | 1.0 |
| 2 | Обученные маппинги (CategoryMapping в БД) | из БД |
| 3 | Точное совпадение (CSV_TO_WB_EXACT_MAPPING) | 0.95 |
| 4 | Контекстный поиск (title + brand + description) | 0.6-0.85 |
| 5 | Fuzzy matching (SequenceMatcher, порог 0.65) | по ratio |
| 6 | Keyword match | 0.5-0.75 |
| 7 | Дефолт | 0.3 |

**Обратная связь:** при ручном исправлении категории автоматически вызывается `learn_from_correction()`, создающий CategoryMapping для будущих товаров.

```python
subj_id, subj_name, confidence = SmartCategoryMapper.map_category(
    csv_category='Вибраторы', product_title='LELO Soraya 2',
    brand='LELO', description='...', source_type='sexoptovik'
)
```

---

### AIParsingCache

**Файл:** `services/ai_parsing_cache.py`

Кэширование AI-результатов на основе SHA-256 хэша входных данных.

- Если хэш совпадает с сохраненным `content_hash` - возвращает кэш из `ai_parsed_data_json`
- Если данные изменились - запускает AI-парсинг и сохраняет результат + хэш
- Экономия 50-80% расходов на AI при повторных синхронизациях

```python
cache = AIParsingCache()
result = cache.get_or_parse(product, parse_function)
```

---

### ParsingConfidenceScorer

**Файл:** `services/parsing_confidence.py`

Расчёт оценки качества заполнения товара (0.0-1.0).

**Формула:**
- Базовый балл: 1.0
- Штрафы за отсутствие обязательных полей (title: -0.15, vendor_code: -0.1, photos: -0.1, brand: -0.08, ...)
- Штрафы за fuzzy-matched значения
- Бонус за полноту заполнения

**Уровни качества:**
- >= 0.8 - высокое (зелёный)
- >= 0.5 - среднее (жёлтый)
- >= 0.3 - низкое (оранжевый)
- < 0.3 - критическое (красный)

---

### PhotoURLVerifier

**Файл:** `services/photo_url_verifier.py`

Пакетная проверка доступности URL фотографий.

- HEAD-запрос к каждому URL (fallback на GET при 405)
- Проверка HTTP-статуса (>= 400 = битая ссылка)
- Проверка Content-Type (должен быть image/*)
- Параллельная проверка (ThreadPoolExecutor, до 10 потоков)
- Поддержка HTTP Basic Auth

```python
result = PhotoURLVerifier.verify_supplier_photos(supplier_id=1, limit=200)
# result.total_broken_urls, result.products_with_broken_photos
```

---

## Дашборд качества

**URL:** `/admin/suppliers/<id>/parsing-quality/dashboard`

Интерактивный дашборд с Alpine.js для мониторинга качества парсинга.

**Компоненты:**
- Карточки распределения quality (high/medium/low/critical) с progress-bar
- Fill rates по полям с цветовой кодировкой
- Статистика AI кэша (покрытие, хиты/миссы)
- История парсингов (таблица parsing_logs)
- Кнопка проверки фото

**Кнопка доступа:** на странице товаров поставщика `/admin/suppliers/<id>/products` - кнопка "Качество парсинга".

---

## API эндпоинты

| Метод | URL | Назначение |
|-------|-----|------------|
| GET | `/admin/suppliers/<id>/parsing-quality` | JSON: метрики качества |
| GET | `/admin/suppliers/<id>/parsing-quality/dashboard` | HTML: дашборд |
| POST | `/admin/suppliers/<id>/verify-photos` | JSON: запуск проверки фото |

---

## Миграция БД

**Файл:** `migrations/add_parsing_quality_fields.py`

**Новые поля:**
- `suppliers.csv_column_mapping` (TEXT/JSON) - маппинг колонок
- `suppliers.csv_has_header` (BOOLEAN) - заголовок в CSV
- `supplier_products.parsing_confidence` (REAL) - оценка 0.0-1.0
- `supplier_products.normalization_applied` (BOOLEAN) - нормализация

**Новая таблица** `parsing_logs`:
- supplier_id, event_type, created_at
- total_products, processed_successfully, errors_count
- duration_seconds, field_fill_rates (JSON)
- ai_tokens_used, ai_cache_hits, ai_cache_misses
- errors_json, normalization_stats

**Запуск:**

```bash
# Локально
python migrations/add_parsing_quality_fields.py

# В Docker
docker exec -it seller-platform python3 -c "
from migrations.add_parsing_quality_fields import run_migration
run_migration('/app/data/seller_platform.db')
"
```

---

## Список файлов

| Файл | Тип | Описание |
|------|-----|----------|
| `services/csv_pre_validator.py` | Новый | Предвалидация CSV |
| `services/data_normalizer.py` | Новый | Нормализация данных |
| `services/smart_category_mapper.py` | Новый | Умный маппинг категорий |
| `services/ai_parsing_cache.py` | Новый | Кэширование AI |
| `services/parsing_confidence.py` | Новый | Скоринг качества |
| `services/description_enricher.py` | Новый | Обогащение из описания |
| `services/auto_correction_rules.py` | Новый | Движок автокоррекции |
| `services/photo_url_verifier.py` | Новый | Проверка URL фото |
| `templates/admin_supplier_parsing_quality.html` | Новый | Дашборд качества |
| `migrations/add_parsing_quality_fields.py` | Новый | Миграция БД |
| `services/supplier_service.py` | Изменён | Интеграция pipeline |
| `routes/suppliers.py` | Изменён | Новые эндпоинты |
| `routes/auto_import.py` | Изменён | Обратная связь из коррекций |
| `templates/admin_supplier_products.html` | Изменён | Кнопка дашборда |
