# Руководство по миграции базы данных

## Проблема

После обновления до версии с API Wildberries, база данных требует миграции для добавления новых полей и таблиц.

**Ошибка:**
```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such column: sellers.api_last_sync
```

## Решение

### Автоматическая миграция (Docker)

Если вы используете Docker Compose, миграция выполняется **автоматически** при запуске контейнера.

```bash
# Остановить контейнеры
docker-compose down

# Пересобрать образы с новым кодом
docker-compose build

# Запустить (миграция выполнится автоматически)
docker-compose up -d seller-platform

# Проверить логи миграции
docker-compose logs seller-platform
```

Вы должны увидеть в логах:
```
🚀 Инициализация seller-platform...
📦 Применение миграций базы данных...
📝 Проверка таблицы sellers...
  ➕ Добавление колонки: api_last_sync
  ➕ Добавление колонки: api_sync_status
✅ Миграция успешно завершена!
```

### Ручная миграция (без Docker)

Если вы запускаете приложение напрямую через Python:

```bash
# 1. Остановить приложение
# Ctrl+C или kill процесс

# 2. Создать резервную копию БД (на всякий случай)
cp data/seller_platform.db data/seller_platform.db.backup

# 3. Запустить миграцию
python migrate_db.py --db-path data/seller_platform.db

# 4. Запустить приложение
python seller_platform.py
```

### Проверка после миграции

#### 1. Проверить структуру таблицы sellers

```bash
# С Docker
docker exec seller-platform sqlite3 /app/data/seller_platform.db ".schema sellers"

# Без Docker
sqlite3 data/seller_platform.db ".schema sellers"
```

Должны быть поля:
- `api_last_sync DATETIME`
- `api_sync_status VARCHAR(50)`

#### 2. Проверить наличие новых таблиц

```bash
# С Docker
docker exec seller-platform sqlite3 /app/data/seller_platform.db ".tables"

# Без Docker
sqlite3 data/seller_platform.db ".tables"
```

Должны быть таблицы:
- `products` - карточки товаров WB
- `api_logs` - логи API запросов
- `seller_reports` - история отчетов

#### 3. Проверить индексы

```bash
# С Docker
docker exec seller-platform sqlite3 /app/data/seller_platform.db ".indexes products"

# Без Docker
sqlite3 data/seller_platform.db ".indexes products"
```

Должны быть индексы:
- `idx_products_seller_id`
- `idx_products_nm_id`
- `idx_products_vendor_code`
- `idx_seller_nm_id`
- `idx_seller_vendor_code`
- `idx_seller_active`

## Что добавляет миграция

### Таблица sellers
**Новые колонки:**
- `api_last_sync` - время последней синхронизации с WB API
- `api_sync_status` - статус синхронизации (success, syncing, error, etc.)

### Таблица products (новая)
Карточки товаров из WB API:
- `nm_id` - артикул WB (nmID)
- `vendor_code` - артикул поставщика
- `title`, `brand`, `object_name` - информация о товаре
- `price`, `discount_price`, `quantity` - цены и остатки
- `photos_json`, `sizes_json` - медиа и размеры
- Индексы для быстрого поиска по seller_id, nm_id, vendor_code

### Таблица api_logs (новая)
Логи всех API-запросов к WB:
- `endpoint`, `method`, `status_code` - детали запроса
- `response_time` - время выполнения
- `success`, `error_message` - результат
- Индексы для быстрой фильтрации по seller_id и времени

### Таблица seller_reports (новая)
История расчетов прибыли:
- `statistics_path`, `price_path`, `processed_path` - пути к файлам
- `selected_columns` - выбранные колонки
- `summary` - сводка по отчету

## Откат миграции

Если что-то пошло не так, вы можете откатиться к резервной копии:

```bash
# С Docker
docker-compose down
cp data/seller_platform.db.backup data/seller_platform.db
docker-compose up -d seller-platform

# Без Docker
cp data/seller_platform.db.backup data/seller_platform.db
python seller_platform.py
```

## Troubleshooting

### Ошибка "database is locked"

```bash
# Остановить все процессы, использующие БД
docker-compose down
# или
pkill -f "python.*seller_platform"

# Запустить снова
docker-compose up -d seller-platform
```

### Миграция не применяется

```bash
# Удалить БД и создать заново (ОСТОРОЖНО: потеряете данные!)
rm data/seller_platform.db
docker-compose up -d seller-platform

# Или запустить миграцию вручную
docker exec seller-platform python migrate_db.py --db-path /app/data/seller_platform.db
```

### Ошибка "no such table: products"

Скорее всего миграция не выполнилась. Проверьте логи:

```bash
docker-compose logs seller-platform | grep -A 20 "миграция"
```

Или запустите миграцию вручную:

```bash
docker exec seller-platform python migrate_db.py --db-path /app/data/seller_platform.db
```

## Настройка ENCRYPTION_KEY

Для шифрования API ключей WB нужен ENCRYPTION_KEY:

```bash
# 1. Сгенерировать ключ
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Добавить в .env файл
echo "ENCRYPTION_KEY=сгенерированный-ключ" >> .env

# 3. Перезапустить контейнер
docker-compose restart seller-platform
```

**ВАЖНО:** Если вы уже сохранили API ключи без шифрования, они будут работать, но не будут зашифрованы. Чтобы зашифровать их:

1. Сохраните старый ключ
2. Установите ENCRYPTION_KEY
3. Перезайдите в настройки API и введите ключ заново

## Дополнительная информация

- **Скрипт миграции:** `migrate_db.py`
- **Документация API:** `WB_API_SETUP.md`
- **Docker entrypoint:** `docker-entrypoint.sh` (запускает миграцию автоматически)

---

**Версия:** 1.0.0
**Дата:** 2025-11-03
