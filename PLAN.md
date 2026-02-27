# План: Синхронизация остатков/цен и генерация цен для товаров поставщика

## Контекст

CSV файл sexoptovik (`all_prod_prices__.csv`) содержит:
```
id товара;осн. артикул;цена;наличие;статус;доп. артикул;штрихкод;ррц
```
- **цена** — закупочная цена поставщика
- **наличие** — остаток (количество)
- **статус** — статус товара у поставщика (0/1)
- **ррц** — рекомендованная розничная цена
- **штрихкод** — штрихкод товара
- `.inf` файл — содержит timestamp + hash для определения обновлений (файл обновляется нон-стоп)

Уже существует `SupplierPriceLoader` в `pricing_engine.py` (парсит id, vendor_code, price, quantity).
Уже существует `calculate_price()` — формула расчёта розничных цен из закупочных.

## Шаг 1: Расширение модели Supplier (models.py)

Добавить поля для URL файла цен/остатков и настроек синхронизации:

```python
# В класс Supplier — ПОСЛЕ csv_encoding:
# Источник цен и остатков (отдельный файл)
price_file_url = db.Column(db.String(500))       # URL CSV цен/остатков
price_file_inf_url = db.Column(db.String(500))    # URL INF файла (change detection)
price_file_delimiter = db.Column(db.String(5), default=';')
price_file_encoding = db.Column(db.String(20), default='cp1251')

# Статистика синхронизации цен
last_price_sync_at = db.Column(db.DateTime)
last_price_sync_status = db.Column(db.String(50))  # success/failed/running
last_price_sync_error = db.Column(db.Text)
last_price_file_hash = db.Column(db.String(64))     # MD5 hash INF файла

# Автосинхронизация
auto_sync_prices = db.Column(db.Boolean, default=False)
auto_sync_interval_minutes = db.Column(db.Integer, default=60)  # интервал проверки
```

## Шаг 2: Расширение модели SupplierProduct (models.py)

Добавить поля для полноценного товара:

```python
# В класс SupplierProduct — ПОСЛЕ currency:
recommended_retail_price = db.Column(db.Float)   # РРЦ от поставщика
supplier_status = db.Column(db.String(50))        # Статус у поставщика (in_stock/out_of_stock)
additional_vendor_code = db.Column(db.String(200)) # Доп. артикул

# Синхронизация цен/остатков
last_price_sync_at = db.Column(db.DateTime)       # Когда последний раз обновилась цена
price_changed_at = db.Column(db.DateTime)          # Когда цена реально изменилась
previous_price = db.Column(db.Float)               # Предыдущая цена (для трекинга)
```

## Шаг 3: Миграция БД

Создать миграцию для добавления новых полей (Flask-Migrate или raw SQL через `db.create_all()`).

## Шаг 4: Сервис синхронизации цен/остатков (supplier_service.py)

Новый метод `sync_prices_and_stock()`:

```python
@staticmethod
def sync_prices_and_stock(supplier_id: int, force: bool = False) -> SyncResult:
    """
    Синхронизация цен и остатков из отдельного CSV файла поставщика.

    Логика:
    1. Проверить INF файл — обновился ли (если не force)
    2. Скачать CSV цен (all_prod_prices__.csv)
    3. Распарсить: id, артикул, цена, наличие, статус, доп.артикул, штрихкод, ррц
    4. Для каждого товара по external_id:
       - Обновить supplier_price (с сохранением previous_price)
       - Обновить supplier_quantity
       - Обновить supplier_status
       - Обновить recommended_retail_price
       - Обновить barcode (из штрихкод)
       - Обновить additional_vendor_code
       - Обновить vendor_code (из осн. артикул)
    5. Сохранить hash INF файла
    """
```

Новый парсер `SupplierPriceCSVParser` (расширение существующего `SupplierPriceLoader`):
- Парсит все 8 колонок CSV
- Возвращает `Dict[str, Dict]` с полными данными

## Шаг 5: Расчёт цен на уровне поставщика (supplier_service.py)

Новый метод для расчёта рекомендованных розничных цен для всех товаров поставщика:

```python
@staticmethod
def calculate_supplier_prices(supplier_id: int, pricing_settings: dict = None) -> dict:
    """
    Рассчитать розничные цены для товаров поставщика на основе pricing_engine.
    Использует default_markup_percent поставщика или переданные настройки.
    Возвращает статистику: сколько рассчитано, диапазон цен.
    """
```

А также метод получения статистики по ценам/остаткам для дашборда:

```python
@staticmethod
def get_price_stock_stats(supplier_id: int) -> dict:
    """
    Вернуть:
    - total, in_stock, out_of_stock
    - with_price, without_price
    - min_price, max_price, avg_price
    - total_stock_quantity
    - last_price_sync_at
    """
```

## Шаг 6: Маршруты (routes/suppliers.py)

### Новые эндпоинты:

```python
# Ручная синхронизация цен/остатков
POST /admin/suppliers/<id>/sync-prices

# API для получения статуса синхронизации (AJAX)
GET /admin/suppliers/<id>/sync-prices/status

# Массовый расчёт розничных цен
POST /admin/suppliers/<id>/calculate-prices
```

### Обновить `_extract_supplier_form_data`:
Добавить обработку новых полей: `price_file_url`, `price_file_inf_url`, `auto_sync_prices`, `auto_sync_interval_minutes`.

### Обновить `create_supplier` и `update_supplier`:
Добавить новые поля в `simple_fields`.

## Шаг 7: Шаблон формы поставщика (admin_supplier_form.html)

В таб "Источник данных" добавить секцию **"Файл цен и остатков"**:

```
--- Файл цен и остатков ---
[URL файла цен]         all_prod_prices__.csv
[URL INF файла]         all_prod_prices__.inf
[Разделитель]  [Кодировка]

[x] Автоматическая синхронизация
[Интервал проверки (мин)]  60

Последняя синхронизация: 27.02.2026 11:16  (✓ успешно)
[Кнопка: Синхронизировать сейчас]
```

В сайдбар статистики добавить блок **"Остатки и цены"**:
```
В наличии:    8 234
Нет в наличии: 7 259
С ценой:      8 234
Мин. цена:    47 ₽
Макс. цена:   12 500 ₽
Послед. синхр: 27.02 11:16
```

## Шаг 8: Шаблон списка товаров (admin_supplier_products.html)

Добавить колонки в таблицу:
- **Остаток** — с цветовой индикацией (зелёный если >0, серый если 0)
- **РРЦ** — рекомендованная розничная цена

Добавить кнопку **"Синхр. цены/остатки"** рядом с "Синхронизация CSV".

Добавить фильтр **"Наличие"** (все / в наличии / нет в наличии).

## Шаг 9: Шаблон детали товара (admin_supplier_product_detail.html)

Расширить секцию цен:
```
Цена поставщика: [1250] ₽      Остаток: [5]
РРЦ: [2990] ₽                  Статус: В наличии ✓
Доп. артикул: [ABC-123]        Штрихкод: [4607123456789]

--- Расчёт розничной цены ---
Закупочная: 1 250 ₽
Прибыль Q: 312 ₽ (25%)
Цена на МП (Z): 3 847 ₽
Цена со скидкой (X): 3 654 ₽
Цена до скидки (Y): 5 963 ₽
```

## Шаг 10: Обновление `sync_from_csv` для интеграции с ценами

Текущий `sync_from_csv` при синхронизации каталога принимает `price_data` параметр, но не использует файл цен автоматически.

Обновить: после синхронизации каталога — **автоматически** запускать `sync_prices_and_stock()` если `price_file_url` задан. Это гарантирует что новые товары сразу получат цену и остаток.

## Шаг 11: Каскадное обновление к продавцам

При синхронизации цен/остатков — обновлять `ImportedProduct` у всех подключённых продавцов:

```python
# После sync_prices_and_stock():
# Для каждого обновлённого SupplierProduct:
#   Найти все ImportedProduct с этим supplier_product_id
#   Обновить supplier_price, supplier_quantity
#   НЕ менять calculated_price — это делает продавец через свои PricingSettings
```

Продавец видит обновлённые закупочные цены и может пересчитать свои розничные через существующий механизм `PricingSettings + calculate_price()`.

---

## Архитектура потока данных

```
sexoptovik CSV каталог ──→ sync_from_csv() ──→ SupplierProduct (title, brand, photos...)
                                                       │
sexoptovik CSV цен ──→ sync_prices_and_stock() ──→ SupplierProduct (price, qty, rrp, status)
      │                                                │
      │  .inf file ─→ check_update()                   ├──→ ImportedProduct.supplier_price (каскад)
      │                                                │    (calculated_price НЕ меняется)
      │                                                │
      └── auto_sync ─→ periodic check                 └──→ Admin UI: цены, остатки, расчёт

Продавец:
  ImportedProduct.supplier_price ──→ PricingSettings + calculate_price() ──→ calculated_price
  (Продавец может менять PricingSettings и пересчитывать цены сам)
```

## Порядок реализации

1. models.py — новые поля Supplier + SupplierProduct
2. supplier_service.py — `sync_prices_and_stock()`, `get_price_stock_stats()`
3. routes/suppliers.py — новые эндпоинты + обновление helpers
4. admin_supplier_form.html — секция цен/остатков
5. admin_supplier_products.html — колонки остаток + ррц + кнопка синхр
6. admin_supplier_product_detail.html — расширенная секция цен + расчёт
7. Интеграция sync_from_csv + sync_prices_and_stock
8. Каскадное обновление ImportedProduct
