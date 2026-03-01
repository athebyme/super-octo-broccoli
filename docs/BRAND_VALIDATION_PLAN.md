# Brand Validation Engine — План реализации

## Проблема

На WB строгая валидация брендов. У поставщика в CSV может быть `LOVETOYS`, а на WB зарегистрировано `Lovetoys` или `Lovetoys Toy`. Текущая система имеет базовые механизмы матчинга (`BrandCache`, `DataNormalizer.normalize_brand`), но они:

1. **Реактивные** — валидация происходит только в момент импорта, ошибки обнаруживаются слишком поздно
2. **Без централизованного хранилища** — маппинги брендов (supplier → WB) хранятся в коде (`BRAND_CANONICAL` dict на ~25 записей)
3. **Без UI управления** — администратор не может добавлять/редактировать маппинги без деплоя
4. **Без учёта категорий** — один и тот же бренд может быть допустим в одной категории WB и недопустим в другой (`GET /api/content/v1/brands?subjectId=...`)
5. **Без периодической ревалидации** — бренды на WB меняются (добавляются, переименовываются), а существующие карточки не перепроверяются
6. **Без единого статуса** — нет чёткого lifecycle для бренда (новый → проверен → подтверждён → отклонён)

## Анализ WB API

### Ключевые эндпоинты

| Метод | Эндпоинт | Назначение |
|-------|----------|------------|
| `GET` | `/content/v2/directory/brands?name=...&top=N` | Поиск брендов по части имени (используется в `wb_api_client.py:1767`) |
| `GET` | `/api/content/v1/brands?subjectId=...` | Бренды доступные для конкретной категории (subjectId) |
| `POST` | `/content/v2/cards/upload` | Создание карточки — поле `brand` (string) на уровне variant |
| `POST` | `/content/v2/cards/update` | Обновление карточки — **brand обязателен**, если не передать — WB обнулит |
| `GET` | `/content/v2/cards/error/list` | Ошибки при создании карточек (в т.ч. по бренду) |

### Особенности WB

- **Бренд — строка**, не ID. Должен **точно** совпадать с каноническим именем в справочнике WB
- **Case-sensitive** при поиске через API (хотя принимает разные регистры, поиск возвращает разные результаты)
- **Зависимость от категории**: `GET /api/content/v1/brands?subjectId=...` возвращает только бренды допустимые для категории
- **Модерация новых брендов**: заявка рассматривается до 2 рабочих дней
- **Нельзя удалять brand при update**: если поле отсутствует, WB обнуляет бренд на карточке (см. `wb_validators.py:225`)

## Архитектура решения

### Слой 1: Модель данных (Brand Registry)

```
Brand (центральный реестр)
├── id (PK)
├── name                    — каноническое имя (как на WB)
├── wb_brand_id             — ID бренда в справочнике WB (nullable)
├── status                  — 'pending' | 'verified' | 'rejected' | 'needs_review'
├── country                 — страна бренда (для auto_correction_rules)
├── verified_at             — когда последний раз подтверждён через WB API
├── created_at / updated_at
└── notes                   — комментарий админа

BrandAlias (маппинги вариантов)
├── id (PK)
├── brand_id (FK → Brand)
├── alias                   — вариант написания ("LOVETOYS", "lovetoys", "Love Toys")
├── alias_normalized        — нормализованная версия (lowercase, без спецсимволов)
├── source                  — 'manual' | 'ai_detected' | 'supplier_csv' | 'auto_matched'
├── confidence              — уверенность в маппинге (0-1), для auto_matched
├── supplier_id (FK, nullable) — от какого поставщика пришёл alias
├── created_at
└── is_active               — можно деактивировать без удаления

BrandCategoryLink (допустимость бренда в категории WB)
├── id (PK)
├── brand_id (FK → Brand)
├── wb_subject_id           — ID предмета WB
├── wb_subject_name         — название для UI
├── verified_at             — когда проверено через API
└── is_available            — доступен ли бренд для этой категории
```

### Слой 2: Brand Validation Engine (сервис)

```python
# services/brand_engine.py

class BrandEngine:
    """Центральный движок валидации и резолва брендов."""

    def resolve(self, raw_brand: str, subject_id: int = None) -> BrandResolution:
        """
        Главный метод — резолвит сырой бренд от поставщика в каноническое имя WB.

        Pipeline:
        1. Нормализация (strip, unicode normalize)
        2. Exact match по BrandAlias.alias_normalized
        3. Fuzzy match по BrandAlias (SequenceMatcher, threshold 0.85)
        4. Поиск в BrandCache (in-memory кэш WB брендов)
        5. Если subject_id — проверка допустимости бренда в категории
        6. Если ничего не найдено → статус 'unresolved', создание Brand(status='pending')

        Returns:
            BrandResolution(
                status='exact'|'confident'|'uncertain'|'unresolved',
                brand=Brand,           # найденный бренд или None
                canonical_name=str,    # имя для WB API
                confidence=float,
                suggestions=[Brand],   # альтернативы
                category_valid=bool,   # допустим ли в указанной категории
            )
        """

    def validate_brand_for_category(self, brand: Brand, subject_id: int) -> bool:
        """Проверить допустимость бренда в категории WB через API."""

    def bulk_resolve(self, items: List[dict]) -> List[BrandResolution]:
        """Пакетный резолв для импорта CSV (оптимизация — один проход по кэшу)."""

    def sync_wb_brands(self, wb_client) -> SyncResult:
        """
        Полная синхронизация справочника WB в БД.
        Заменяет текущий BrandCache.sync_brands().
        Результат — Brand записи в БД + BrandCategoryLink обновления.
        """

    def create_brand_from_alias(self, alias: str, source: str) -> Brand:
        """Создать новый бренд из неизвестного alias (status='pending')."""

    def merge_brands(self, source_brand_id: int, target_brand_id: int):
        """Объединить дубликаты (перенести aliases, обновить products)."""
```

### Слой 3: Интеграция с существующим кодом

#### 3.1 Замена `BRAND_CANONICAL` dict

Текущий `data_normalizer.py:22` содержит захардкоженный dict на ~25 записей.
Заменяется на `BrandEngine.resolve()`, который ищет по `BrandAlias`.

**Миграция**: все записи из `BRAND_CANONICAL` импортируются как `Brand` + `BrandAlias` записи при первом запуске.

#### 3.2 Замена `BrandCache` (services/brand_cache.py)

Текущий `BrandCache` — Singleton с in-memory хранилищем, синхронизируемый раз в час.
Заменяется на:
- **БД-хранилище** (`Brand` таблица) — как source of truth
- **In-memory кэш** — `BrandEngine._cache` с тем же TTL, но подкреплённый БД
- **`BrandAlias` индекс** — быстрый lookup по `alias_normalized` (DB index + in-memory dict)

#### 3.3 Обновление Auto-Import Pipeline

```
CSVParser → DataNormalizer → [BrandEngine.resolve()] → ProductValidator → WB API
                                      ↓
                              resolve → uncertain?
                                      ↓
                              ImportedProduct.brand_status = 'needs_review'
                              ImportedProduct.brand_suggestions = JSON
```

В `auto_import_manager.py:825` (`ProductValidator`):
- Вместо простой проверки `if not product_data.get('brand')` — вызов `BrandEngine.resolve()`
- Если `status == 'unresolved'` → product помечается `needs_review`, не блокирует импорт
- Если `status == 'confident'` → автоматическая замена на каноническое имя

#### 3.4 Обновление AI парсинга

В `ai_service.py:detect_brand()` и `marketplace_ai_parser.py`:
- AI-определённый бренд проходит через `BrandEngine.resolve()`
- Если бренд новый — создаётся `Brand(status='pending')` + `BrandAlias(source='ai_detected')`
- Если уверенность AI > 0.8 и есть `confident` match — автоматический маппинг

#### 3.5 Обновление WB Card Update

В `wb_validators.py:prepare_card_for_update()`:
- Перед отправкой: `BrandEngine.resolve(brand, subject_id)` → проверка category_valid
- Если бренд не допустим в категории → warning в логах + возможность блокировки

### Слой 4: Admin UI — Управление брендами

#### 4.1 Список брендов `/admin/brands`

```
┌─────────────────────────────────────────────────────────────────────┐
│ Бренды                                        [Синхр. с WB] [+ Добавить]│
├─────────────────────────────────────────────────────────────────────┤
│ Поиск: [________________]   Статус: [Все ▼]   Источник: [Все ▼]   │
├─────────────────────────────────────────────────────────────────────┤
│ ●  LELO              WB: ✅  Алиасы: 3   Категорий: 12   [▸]     │
│ ●  Satisfyer         WB: ✅  Алиасы: 2   Категорий: 8    [▸]     │
│ ⚠  LoveToys          WB: ❓  Алиасы: 5   Категорий: 0    [▸]     │
│ ○  NewBrandXYZ       WB: ❌  Алиасы: 1   Категорий: 0    [▸]     │
└─────────────────────────────────────────────────────────────────────┘
```

- **Статусы**: ● verified (зелёный), ⚠ needs_review (жёлтый), ○ pending (серый), ✕ rejected (красный)
- **Фильтры**: по статусу, по поставщику, по наличию в WB, по количеству товаров
- **Массовые действия**: "Проверить все pending через WB API", "Объединить выбранные"

#### 4.2 Карточка бренда `/admin/brands/<id>`

```
┌─────────────────────────────────────────────────────────────────────┐
│ LELO                                    Статус: [Verified ▼]       │
│ WB Brand ID: 14523       Страна: Швеция                            │
│ Последняя проверка: 2 часа назад                                   │
├─────────────────────────────────────────────────────────────────────┤
│ АЛИАСЫ:                                            [+ Добавить]    │
│   "LELO"       — manual              ✅ active                     │
│   "lelo"       — supplier_csv        ✅ active                     │
│   "Lelo"       — auto_matched (0.95) ✅ active                     │
│   "LELLO"      — supplier_csv        ✅ active    [Удалить]        │
├─────────────────────────────────────────────────────────────────────┤
│ ДОПУСТИМЫЕ КАТЕГОРИИ WB:                    [Обновить из WB]       │
│   Вибромассажёры (subjectId: 5234)          ✅ проверено            │
│   Массажёры (subjectId: 1892)               ✅ проверено            │
│   Товары для взрослых (subjectId: 7341)     ✅ проверено            │
├─────────────────────────────────────────────────────────────────────┤
│ ТОВАРЫ С ЭТИМ БРЕНДОМ:                                             │
│   ImportedProducts: 45    SupplierProducts: 120    Products: 38    │
│   [Показать товары →]                                              │
├─────────────────────────────────────────────────────────────────────┤
│ ИСТОРИЯ:                                                           │
│   01.03.2026 — Создан (источник: supplier_csv)                     │
│   01.03.2026 — Alias "LELLO" добавлен (supplier: sexoptovik)       │
│   01.03.2026 — Verified через WB API (wb_id: 14523)               │
└─────────────────────────────────────────────────────────────────────┘
```

#### 4.3 Brand Review Queue `/admin/brands/review`

Отдельная страница для быстрого разбора `needs_review` и `pending` брендов:

```
┌─────────────────────────────────────────────────────────────────────┐
│ Очередь проверки брендов              Pending: 12  |  Review: 5    │
├─────────────────────────────────────────────────────────────────────┤
│ "LOVETOYS" (от поставщика sexoptovik, 23 товара)                   │
│                                                                     │
│   Предложения WB:                                                  │
│   ○ Lovetoys         (score: 0.92)  [Выбрать]                     │
│   ○ Lovetoys Toy     (score: 0.78)  [Выбрать]                     │
│   ○ Love Toys Corp   (score: 0.65)  [Выбрать]                     │
│                                                                     │
│   [Подтвердить как "Lovetoys"]  [Создать новый]  [Отклонить]       │
├─────────────────────────────────────────────────────────────────────┤
│ "FunFactory" (от AI, 8 товаров)                                    │
│   → Авто-маппинг на "Fun Factory" (score: 0.95)                   │
│   [Подтвердить] [Отклонить] [Другой вариант]                      │
└─────────────────────────────────────────────────────────────────────┘
```

#### 4.4 Dashboard виджет

На главной `/admin/` — виджет со статистикой:
- Брендов pending / needs_review
- Последняя синхронизация с WB
- Товаров с невалидированным брендом

### Слой 5: Фоновые задачи (APScheduler)

```python
# Периодические задачи для brand_engine

# 1. Синхронизация справочника WB (раз в 6 часов)
scheduler.add_job(
    brand_engine.sync_wb_brands,
    trigger='interval', hours=6,
    id='brand_wb_sync'
)

# 2. Ревалидация существующих брендов (раз в сутки)
# Проверяет Brand(status='verified') — всё ещё существуют в WB?
scheduler.add_job(
    brand_engine.revalidate_existing,
    trigger='cron', hour=3,   # В 3 часа ночи
    id='brand_revalidation'
)

# 3. Авто-resolve pending брендов (раз в час)
# Пытается разрешить Brand(status='pending') через fuzzy + WB API
scheduler.add_job(
    brand_engine.auto_resolve_pending,
    trigger='interval', hours=1,
    id='brand_auto_resolve'
)

# 4. Проверка брендов по категориям (раз в сутки)
# Обновляет BrandCategoryLink — какие бренды доступны в каких категориях
scheduler.add_job(
    brand_engine.sync_brand_categories,
    trigger='cron', hour=4,
    id='brand_category_sync'
)
```

### Слой 6: Интеграция с AI

#### При парсинге

```
AI detect_brand() → raw_brand
       ↓
BrandEngine.resolve(raw_brand, subject_id)
       ↓
  ┌─ exact     → используем canonical_name
  ├─ confident → используем canonical_name + создаём BrandAlias(source='ai_detected')
  ├─ uncertain → ImportedProduct.brand_status='needs_review'
  └─ unresolved → создаём Brand(status='pending') + BrandAlias
```

#### AI-помощник для Review Queue

Для брендов в `needs_review` можно использовать AI для рекомендации:
- Анализ контекста (название товара, категория, описание)
- Предложение наиболее вероятного бренда из WB справочника
- Confidence score для автоматического принятия (>0.95 → auto-approve)

## План реализации по этапам

### Этап 1: Модели и миграция данных

**Файлы:**
- `models.py` — добавить `Brand`, `BrandAlias`, `BrandCategoryLink`
- `migrations/migrate_add_brand_registry.py` — создание таблиц
- `migrations/migrate_seed_brands.py` — импорт из `BRAND_CANONICAL` + существующих брендов

**Задачи:**
1. Создать модели `Brand`, `BrandAlias`, `BrandCategoryLink` в `models.py`
2. Написать миграцию для создания таблиц с индексами
3. Написать seed-миграцию:
   - Импорт из `BRAND_CANONICAL` dict → `Brand` + `BrandAlias`
   - Извлечь уникальные бренды из `ImportedProduct.brand` → `Brand(status='pending')`
   - Извлечь уникальные бренды из `SupplierProduct.brand` → `BrandAlias`
4. Добавить связи:
   - `ImportedProduct.resolved_brand_id` (FK → Brand, nullable)
   - `SupplierProduct.resolved_brand_id` (FK → Brand, nullable)

### Этап 2: Brand Engine (ядро)

**Файлы:**
- `services/brand_engine.py` — новый файл, основной сервис
- `services/brand_cache.py` — рефакторинг: кэш становится подсистемой BrandEngine
- `services/data_normalizer.py` — `normalize_brand()` делегирует в `BrandEngine`

**Задачи:**
1. Реализовать `BrandEngine.resolve()` с 5-шаговым pipeline
2. Реализовать `BrandEngine.bulk_resolve()` для пакетного импорта
3. Реализовать `BrandEngine.sync_wb_brands()` — синхронизация WB → DB
4. Реализовать `BrandEngine.validate_brand_for_category()` — через `GET /api/content/v1/brands?subjectId=...`
5. Реализовать `BrandEngine.merge_brands()` — объединение дубликатов
6. Рефакторить `BrandCache` — использовать DB как source of truth, memory как кэш
7. Обновить `DataNormalizer.normalize_brand()` — делегировать в BrandEngine (с fallback на старую логику)

### Этап 3: Интеграция с Pipeline

**Файлы:**
- `routes/auto_import.py` — обновить валидацию и импорт
- `services/auto_import_manager.py` — обновить `ProductValidator`
- `services/wb_api_client.py` — обновить `validate_brand()`
- `services/marketplace_ai_parser.py` — интегрировать BrandEngine
- `services/ai_service.py` — обновить `detect_brand()` post-processing

**Задачи:**
1. В `ProductValidator.validate_product()`:
   - Вызывать `BrandEngine.resolve()` вместо простой проверки
   - Сохранять `resolved_brand_id` в `ImportedProduct`
   - Помечать `brand_status='needs_review'` если uncertain/unresolved
2. В `auto_import.py` route `/auto-import/<id>/import`:
   - Использовать `BrandEngine` вместо прямого `wb_client.validate_brand()`
   - Сохранять результат резолва в БД
3. В `auto_import.py` route `/auto-import/validate-brand`:
   - Использовать `BrandEngine` вместо прямого вызова API
   - Возвращать историю резолва (какие aliases уже есть)
4. В `marketplace_ai_parser.py`:
   - После AI парсинга → `BrandEngine.resolve()` для валидации
   - Новые бренды от AI → `Brand(status='pending')` + `BrandAlias(source='ai_detected')`
5. В `wb_validators.py:prepare_card_for_update()`:
   - Перед отправкой проверить бренд через `BrandEngine.resolve(brand, subject_id)`

### Этап 4: Admin UI

**Файлы:**
- `routes/brands.py` — новый Blueprint для управления брендами
- `templates/admin_brands.html` — список брендов
- `templates/admin_brand_detail.html` — карточка бренда
- `templates/admin_brand_review.html` — очередь на проверку

**Задачи:**
1. Создать Blueprint `brands` с маршрутами:
   - `GET /admin/brands` — список с фильтрами и поиском
   - `GET /admin/brands/<id>` — карточка бренда (aliases, категории, товары)
   - `POST /admin/brands` — создать бренд вручную
   - `PUT /admin/brands/<id>` — обновить (статус, имя, страна)
   - `POST /admin/brands/<id>/aliases` — добавить alias
   - `DELETE /admin/brands/<id>/aliases/<alias_id>` — удалить alias
   - `POST /admin/brands/<id>/verify` — проверить через WB API
   - `POST /admin/brands/<id>/merge` — объединить с другим
   - `GET /admin/brands/review` — очередь на проверку
   - `POST /admin/brands/review/<id>/approve` — подтвердить маппинг
   - `POST /admin/brands/review/<id>/reject` — отклонить
   - `POST /admin/brands/sync` — синхронизация с WB
   - `GET /admin/brands/stats` — API для dashboard виджета
2. Реализовать шаблоны:
   - `admin_brands.html` — таблица с Alpine.js для фильтрации/поиска
   - `admin_brand_detail.html` — редактирование бренда, aliases, категории
   - `admin_brand_review.html` — интерактивная очередь проверки
3. Добавить виджет на dashboard `admin_dashboard.html`
4. Добавить пункт "Бренды" в навигацию админки

### Этап 5: Фоновые задачи

**Файлы:**
- `seller_platform.py` — регистрация задач в APScheduler
- `services/brand_engine.py` — методы для scheduler

**Задачи:**
1. `sync_wb_brands` — раз в 6 часов, полная синхронизация справочника
2. `revalidate_existing` — раз в сутки, перепроверка verified брендов
3. `auto_resolve_pending` — раз в час, попытка разрешить pending через fuzzy + API
4. `sync_brand_categories` — раз в сутки, обновление BrandCategoryLink

### Этап 6: API для фронтенда

**Файлы:**
- `routes/brands.py` — AJAX-эндпоинты

**Задачи:**
1. `POST /admin/brands/api/resolve` — resolve бренда (для inline-валидации в UI импорта)
2. `GET /admin/brands/api/search?q=...` — autocomplete для бренда (в форме редактирования товара)
3. `GET /admin/brands/api/stats` — статистика для dashboard
4. `POST /admin/brands/api/bulk-resolve` — пакетный резолв (для массовых операций)

## Схема данных (SQL)

```sql
CREATE TABLE brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(200) NOT NULL,                    -- каноническое имя (как на WB)
    name_normalized VARCHAR(200) NOT NULL,          -- lowercase без спецсимволов
    wb_brand_id INTEGER,                            -- ID из справочника WB
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending/verified/rejected/needs_review
    country VARCHAR(100),
    notes TEXT,
    verified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(name_normalized)
);
CREATE INDEX idx_brand_status ON brands(status);
CREATE INDEX idx_brand_wb_id ON brands(wb_brand_id);

CREATE TABLE brand_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    alias VARCHAR(200) NOT NULL,
    alias_normalized VARCHAR(200) NOT NULL,          -- lowercase без спецсимволов
    source VARCHAR(30) NOT NULL DEFAULT 'manual',    -- manual/ai_detected/supplier_csv/auto_matched
    confidence REAL DEFAULT 1.0,
    supplier_id INTEGER REFERENCES suppliers(id),
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(alias_normalized)
);
CREATE INDEX idx_alias_normalized ON brand_aliases(alias_normalized);
CREATE INDEX idx_alias_brand ON brand_aliases(brand_id);
CREATE INDEX idx_alias_source ON brand_aliases(source);

CREATE TABLE brand_category_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    wb_subject_id INTEGER NOT NULL,
    wb_subject_name VARCHAR(200),
    is_available BOOLEAN DEFAULT 1,
    verified_at TIMESTAMP,

    UNIQUE(brand_id, wb_subject_id)
);
CREATE INDEX idx_bcl_brand ON brand_category_links(brand_id);
CREATE INDEX idx_bcl_subject ON brand_category_links(wb_subject_id);
```

## Порядок приоритетов

1. **Этап 1 + 2** (Модели + Engine) — фундамент, без него остальное не работает
2. **Этап 3** (Интеграция) — реальная польза: бренды резолвятся автоматически при импорте
3. **Этап 4** (Admin UI) — управление и мониторинг
4. **Этап 5** (Фоновые задачи) — автоматизация и поддержание актуальности
5. **Этап 6** (API) — удобство для фронтенда и будущих интеграций

## Ожидаемый результат

После реализации:
- **Импорт CSV** → бренд автоматически резолвится в каноническое имя WB
- **AI парсинг** → определённый AI бренд сразу валидируется против WB справочника
- **Админ** → видит все бренды, может быстро разбирать очередь unresolved
- **Фон** → справочник WB актуален, новые бренды автоматически матчатся
- **Категории** → при импорте проверяется допустимость бренда в конкретной категории WB
- **Расширяемость** → архитектура готова для Ozon, Yandex Market (добавить `marketplace_id` в `Brand`/`BrandCategoryLink`)
