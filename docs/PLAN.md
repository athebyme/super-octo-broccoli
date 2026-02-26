# Единая база товаров поставщика — План реализации

## Архитектурный обзор

### Текущее состояние
- Flask + SQLAlchemy + SQLite, Jinja2 шаблоны, Tailwind CSS
- Поставщик (sexoptovik) захардкожен в `AutoImportSettings` и `auto_import_manager.py`
- `ImportedProduct` — товары привязаны к конкретному продавцу, каждый продавец парсит CSV отдельно
- Фотографии кэшируются в `photo_cache.py`, но кэш общий на диске (data/photo_cache/)
- AI-сервис (`ai_service.py`) работает через настройки в `AutoImportSettings` (per-seller)
- Админ-панель: управление продавцами, аудит-логи, системные настройки

### Целевое состояние
- Централизованная база товаров для каждого поставщика (Supplier → SupplierProduct)
- Один продавец может работать с несколькими поставщиками (Seller ↔ Supplier через M2M)
- Импорт товара = копирование из SupplierProduct в ImportedProduct продавца + сохранение FK-связи
- Обновление базы поставщика → возможность обновить данные у всех продавцов
- AI-валидация и генерация на уровне поставщика (один раз для всех продавцов)
- Фотографии привязаны к SupplierProduct (общие для всех продавцов)
- Полный CRUD поставщиков и их товаров в админке

---

## Фаза 1: Модели данных (models.py)

### 1.1 Новая модель `Supplier`
```python
class Supplier(db.Model):
    __tablename__ = 'suppliers'

    id              = Integer, PK
    name            = String(200), not null       # "Sexoptovik", "FixPrice"
    code            = String(50), unique, not null # "sexoptovik", "fixprice" (slug)
    description     = Text                         # Описание поставщика
    website         = String(500)                  # URL сайта
    logo_url        = String(500)                  # Логотип

    # Источник данных
    csv_source_url  = String(500)                  # URL CSV для автоимпорта
    csv_delimiter   = String(5), default=';'
    csv_encoding    = String(20), default='cp1251'
    api_endpoint    = String(500)                   # Для будущей API-интеграции

    # Авторизация для доступа к ресурсам поставщика
    auth_login      = String(200)                  # Логин (если нужна авторизация)
    auth_password   = String(200)                  # Пароль (зашифрованный)

    # AI настройки (централизованные для поставщика)
    ai_enabled       = Boolean, default=False
    ai_provider      = String(50)
    ai_api_key       = String(500)        # Зашифрованный
    ai_api_base_url  = String(500)
    ai_model         = String(100)
    # ... (аналогично полям из AutoImportSettings, но на уровне поставщика)

    # Настройки ценообразования по умолчанию
    default_markup_percent = Float       # Наценка по умолчанию

    # Статус
    is_active       = Boolean, default=True

    # Статистика
    total_products    = Integer, default=0
    last_sync_at      = DateTime
    last_sync_status  = String(50)
    last_sync_error   = Text

    # Метаданные
    created_at      = DateTime
    updated_at      = DateTime
    created_by_user_id = Integer, FK→users.id
```

### 1.2 Новая модель `SupplierProduct` (централизованная база)
```python
class SupplierProduct(db.Model):
    __tablename__ = 'supplier_products'

    id                = Integer, PK
    supplier_id       = Integer, FK→suppliers.id, not null, indexed

    # Идентификация
    external_id       = String(200), indexed    # ID из каталога поставщика
    vendor_code       = String(200)             # Артикул поставщика
    barcode           = String(200)             # Штрихкод

    # Основные данные (нормализованные)
    title             = String(500)
    description       = Text
    brand             = String(200)
    category          = String(200)             # Категория поставщика

    # WB маппинг (определяется AI или вручную)
    wb_category_name  = String(200)
    wb_subject_id     = Integer
    wb_subject_name   = String(200)
    category_confidence = Float, default=0.0

    # Цена и остатки
    supplier_price    = Float                   # Закупочная цена
    supplier_quantity = Integer                  # Остаток у поставщика
    currency          = String(10), default='RUB'

    # Характеристики (нормализованные JSON)
    characteristics_json = Text                 # [{name, value}, ...]
    sizes_json           = Text                 # Размеры
    colors_json          = Text                 # Цвета
    materials_json       = Text                 # Материалы
    dimensions_json      = Text                 # Габариты (д/ш/в/вес)
    gender              = String(50)
    country             = String(100)
    season              = String(50)
    age_group           = String(50)

    # Медиа
    photo_urls_json      = Text                 # Оригинальные URL фото от поставщика
    processed_photos_json = Text                # Обработанные фото (локальные пути)
    video_url            = String(500)

    # AI-обогащённые данные
    ai_seo_title         = String(500)
    ai_description       = Text
    ai_keywords_json     = Text
    ai_bullets_json      = Text
    ai_rich_content_json = Text
    ai_analysis_json     = Text
    ai_validated         = Boolean, default=False  # Прошёл AI валидацию
    ai_validated_at      = DateTime
    ai_validation_score  = Float                   # Оценка качества карточки 0-100
    content_hash         = String(64)              # Хеш контента для отслеживания

    # Оригинальные данные (для отката)
    original_data_json   = Text

    # Статус
    status              = String(50), default='draft'
    # draft → validated → ready → archived
    validation_errors_json = Text

    # Метаданные
    created_at          = DateTime
    updated_at          = DateTime

    # Составные индексы
    __table_args__ = (
        UniqueConstraint('supplier_id', 'external_id', name='uq_supplier_external_id'),
        Index('idx_supplier_product_status', 'supplier_id', 'status'),
        Index('idx_supplier_product_category', 'supplier_id', 'wb_subject_id'),
    )
```

### 1.3 Связующая таблица `SellerSupplier` (M2M)
```python
class SellerSupplier(db.Model):
    __tablename__ = 'seller_suppliers'

    id            = Integer, PK
    seller_id     = Integer, FK→sellers.id, not null
    supplier_id   = Integer, FK→suppliers.id, not null

    # Настройки продавца для этого поставщика
    is_active       = Boolean, default=True
    supplier_code   = String(50)               # Код продавца для артикулов этого поставщика
    vendor_code_pattern = String(200)          # Шаблон артикула

    # Ценообразование для этого поставщика
    custom_markup_percent = Float              # Переопределение наценки

    # Дата подключения
    connected_at    = DateTime

    __table_args__ = (
        UniqueConstraint('seller_id', 'supplier_id', name='uq_seller_supplier'),
    )
```

### 1.4 Модификация `ImportedProduct`
Добавить поля:
```python
    supplier_product_id = Integer, FK→supplier_products.id, nullable=True, indexed
    supplier_id         = Integer, FK→suppliers.id, nullable=True, indexed
    # Существующие поля остаются для обратной совместимости
```

### 1.5 Миграция `migrate_add_supplier_database.py`
- Создание таблиц suppliers, supplier_products, seller_suppliers
- ALTER imported_products: добавить supplier_product_id, supplier_id
- Миграция данных: создать Supplier из существующих AutoImportSettings (sexoptovik)
- Перенос товаров из ImportedProduct (у кого source_type='sexoptovik') → создать SupplierProduct
- Проставить FK связи

---

## Фаза 2: Бэкенд — сервисы и маршруты

### 2.1 Файл `supplier_service.py` — бизнес-логика
```
class SupplierService:
    # CRUD поставщиков
    create_supplier(data) → Supplier
    update_supplier(supplier_id, data) → Supplier
    delete_supplier(supplier_id) → bool
    get_supplier(supplier_id) → Supplier
    list_suppliers(filters) → list

    # Управление базой товаров
    sync_from_csv(supplier_id) → SyncResult
    add_product(supplier_id, data) → SupplierProduct
    update_product(product_id, data) → SupplierProduct
    delete_product(product_id) → bool
    bulk_update_products(supplier_id, product_ids, data) → BulkResult

    # AI операции
    ai_validate_product(product_id) → ValidationResult
    ai_validate_bulk(supplier_id, product_ids) → list
    ai_generate_seo(product_id) → dict
    ai_generate_description(product_id) → str
    ai_analyze_product(product_id) → dict

    # Фото
    sync_photos(supplier_id, product_ids) → PhotoSyncResult

    # Подключение продавцов
    connect_seller(seller_id, supplier_id, settings) → SellerSupplier
    disconnect_seller(seller_id, supplier_id) → bool

    # Импорт к продавцу
    import_to_seller(seller_id, supplier_product_ids) → ImportResult
    update_seller_products(seller_id, supplier_product_ids) → UpdateResult
    get_available_products(seller_id, supplier_id, filters) → list
```

### 2.2 Маршруты администратора (в `seller_platform.py`)
```
# CRUD поставщиков
GET  /admin/suppliers                           → Список поставщиков
GET  /admin/suppliers/add                       → Форма создания
POST /admin/suppliers/add                       → Создание поставщика
GET  /admin/suppliers/<id>/edit                 → Форма редактирования
POST /admin/suppliers/<id>/edit                 → Обновление
POST /admin/suppliers/<id>/delete               → Удаление
POST /admin/suppliers/<id>/toggle-active        → Вкл/выкл

# Управление базой товаров поставщика
GET  /admin/suppliers/<id>/products             → Список товаров (таблица с фильтрами)
GET  /admin/suppliers/<id>/products/<pid>       → Детали товара
GET  /admin/suppliers/<id>/products/<pid>/edit  → Редактирование товара
POST /admin/suppliers/<id>/products/<pid>/edit  → Сохранение
POST /admin/suppliers/<id>/sync                 → Синхронизация с CSV
POST /admin/suppliers/<id>/products/bulk-action → Массовые действия

# AI операции в админке
POST /admin/suppliers/<id>/ai/validate          → AI валидация (одного или нескольких)
POST /admin/suppliers/<id>/ai/generate-seo      → AI SEO генерация
POST /admin/suppliers/<id>/ai/generate-desc     → AI описание
POST /admin/suppliers/<id>/ai/analyze           → AI анализ карточки
GET  /admin/suppliers/<id>/ai/history           → История AI действий

# Управление подключением продавцов
GET  /admin/suppliers/<id>/sellers              → Подключённые продавцы
POST /admin/suppliers/<id>/sellers/connect      → Подключить продавца
POST /admin/suppliers/<id>/sellers/disconnect   → Отключить продавца
```

### 2.3 Маршруты продавца (в `seller_platform.py`)
```
# Каталог поставщика (для продавца)
GET  /supplier-catalog                                → Список доступных поставщиков
GET  /supplier-catalog/<supplier_id>/products         → Товары поставщика (просмотр)
GET  /supplier-catalog/<supplier_id>/products/<pid>   → Детали товара

# Импорт / обновление
POST /supplier-catalog/import                   → Импорт выбранных товаров
POST /supplier-catalog/update                   → Обновить существующие из базы поставщика
GET  /supplier-catalog/import-status            → Статус последнего импорта
```

---

## Фаза 3: Шаблоны (templates/)

### 3.1 Админ-панель поставщиков
- `admin_suppliers.html` — Список всех поставщиков (карточки/таблица с фильтрами)
- `admin_supplier_add.html` — Форма создания поставщика (табы: Основное, CSV источник, AI настройки, Авторизация)
- `admin_supplier_edit.html` — Форма редактирования поставщика
- `admin_supplier_products.html` — Таблица товаров поставщика (фильтры, поиск, пагинация, массовые действия)
- `admin_supplier_product_detail.html` — Детальная карточка товара (фото, характеристики, AI данные, история)
- `admin_supplier_product_edit.html` — Редактирование товара поставщика
- `admin_supplier_sellers.html` — Управление подключёнными продавцами
- `admin_supplier_ai.html` — AI-панель (валидация, генерация, статистика)

### 3.2 Каталог для продавца
- `supplier_catalog.html` — Список доступных поставщиков
- `supplier_catalog_products.html` — Каталог товаров с фильтрами + кнопки "Импорт" / "Обновить"
- `supplier_catalog_product_detail.html` — Детали товара (превью перед импортом)

### 3.3 UI/UX Принципы
- **Единый стиль**: Tailwind CSS, компонентная система как в существующих шаблонах
- **Карточки товаров**: Превью фото, основная информация, статус AI, быстрые действия
- **Прогресс-бары**: Для синхронизации, AI-валидации, массового импорта
- **Уведомления**: Flash-сообщения + inline-статусы (как уже реализовано)
- **Модальные окна**: Подтверждение удаления, настройки подключения
- **Массовые действия**: Checkbox + dropdown для выбора действия (импорт, AI, удаление)
- **Фильтры**: По категории, статусу AI, наличию фото, диапазону цен
- **Responsive**: Адаптивная верстка (как в текущих шаблонах)

---

## Фаза 4: Интеграция с существующим функционалом

### 4.1 Фото (photo_cache.py)
- Текущий кэш `data/photo_cache/{supplier_type}/{external_id}/` остаётся
- SupplierProduct.processed_photos_json → ссылки на кэшированные фото
- При импорте к продавцу: ImportedProduct.processed_photos = SupplierProduct.processed_photos (копирование ссылок, не файлов)
- Для WB загрузки: фото берутся из кэша поставщика

### 4.2 AI (ai_service.py)
- AI настройки хранятся в Supplier (централизованно)
- AI операции доступны из админки на уровне товаров поставщика
- Продавец может переопределить AI данные у себя (если хочет)
- Новый метод в AI сервисе: `validate_supplier_product()`, `enrich_supplier_product()`

### 4.3 Ценообразование (pricing_engine.py)
- SupplierProduct.supplier_price → базовая цена
- При импорте к продавцу: применяется формула PricingSettings продавца
- Seller может переопределить цену для конкретных товаров

### 4.4 Обратная совместимость
- Существующие ImportedProduct с source_type='sexoptovik' продолжают работать
- Автоимпорт через AutoImportSettings продолжает работать (deprecated, но не ломается)
- Новый флоу: Supplier → SupplierProduct → import_to_seller → ImportedProduct
- Постепенная миграция: admin может "перенести" данные из старого формата в новый

### 4.5 Навигация
- В шапку админки добавить пункт "Поставщики"
- В dashboard продавца добавить карточку "Каталог поставщиков"
- В admin.html добавить блок статистики по поставщикам

---

## Фаза 5: Безопасность

- Все маршруты поставщиков в админке — `@login_required` + проверка `is_admin`
- Маршруты каталога для продавца — `@login_required` + проверка подключения к поставщику
- Шифрование API ключей и паролей через Fernet (как для wb_api_key)
- CSRF-токены для всех POST-форм
- Аудит-лог: все действия с поставщиками → AdminAuditLog
- Валидация входных данных: все формы и API эндпоинты
- Ограничение массовых операций (rate limiting при AI)
- SQL injection prevention (SQLAlchemy ORM)

---

## Порядок реализации (последовательность коммитов)

### Коммит 1: Модели и миграции
- Новые модели в models.py (Supplier, SupplierProduct, SellerSupplier)
- Модификация ImportedProduct (новые FK)
- Скрипт миграции migrate_add_supplier_database.py

### Коммит 2: Сервисный слой
- supplier_service.py с полной бизнес-логикой
- Тесты для сервисного слоя

### Коммит 3: Админ маршруты — CRUD поставщиков
- Маршруты в seller_platform.py
- Шаблоны: admin_suppliers.html, admin_supplier_add.html, admin_supplier_edit.html
- Навигация в admin.html

### Коммит 4: Админ маршруты — управление товарами поставщика
- Маршруты для товаров
- Шаблоны: admin_supplier_products.html, admin_supplier_product_detail.html, admin_supplier_product_edit.html
- Синхронизация CSV

### Коммит 5: AI интеграция на уровне поставщика
- AI маршруты в админке
- admin_supplier_ai.html
- Интеграция с ai_service.py

### Коммит 6: Каталог для продавца + импорт
- Маршруты каталога для продавца
- Шаблоны: supplier_catalog.html, supplier_catalog_products.html
- Логика импорта и обновления

### Коммит 7: Интеграция, навигация, полировка
- Обновление dashboard и навигации
- Обновление admin.html (статистика поставщиков)
- Проверка обратной совместимости
