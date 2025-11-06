# Инструкция по применению миграции для subject_id

## Проблема

После обновления кода возникает ошибка:
```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such column: products.subject_id
```

Это происходит потому что в модель `Product` добавлено новое поле `subject_id`, но база данных еще не обновлена.

## Решение

### Вариант 1: Автоматическое применение миграции (рекомендуется)

```bash
python3 apply_migrations.py
```

Этот скрипт автоматически:
- Определит путь к базе данных из переменной окружения `DATABASE_URL` или использует `data/seller_platform.db`
- Проверит существует ли колонка `subject_id`
- Добавит колонку если её нет

### Вариант 2: Применение миграции в Docker контейнере

```bash
# Запустить миграцию внутри контейнера
docker exec -it seller-platform python3 /app/apply_migrations.py
```

### Вариант 3: Ручное применение миграции

```bash
python3 migrate_add_subject_id.py data/seller_platform.db
```

### Вариант 4: Пересоздание базы данных (удаляет все данные!)

**⚠️ ВНИМАНИЕ: Это удалит все данные в базе!**

```bash
# Удалить старую базу
rm data/seller_platform.db

# Создать новую базу с правильной схемой
python3 init_platform.py
```

## Что делает миграция

Миграция добавляет поле `subject_id` в таблицу `products`:

```sql
ALTER TABLE products ADD COLUMN subject_id INTEGER;
```

Это поле используется для:
- Хранения ID предмета (subject) из WB API
- Быстрого получения характеристик товара через правильный API endpoint
- Исправления проблемы с загрузкой характеристик

## После миграции

1. **Перезапустите приложение**:
   ```bash
   # Для Docker
   docker-compose restart seller-platform

   # Для локального запуска
   # Остановите приложение (Ctrl+C) и запустите снова
   python3 seller_platform.py
   ```

2. **Синхронизируйте товары**:
   - Зайдите в веб-интерфейс
   - Перейдите в раздел "Товары"
   - Нажмите кнопку "Синхронизация"
   - После синхронизации поле `subject_id` будет заполнено для всех товаров

3. **Проверьте работу**:
   - Попробуйте отредактировать товар
   - Характеристики должны загружаться корректно
   - Массовое редактирование должно работать без ошибок

## Проверка успешности миграции

После применения миграции можно проверить что колонка добавлена:

```bash
sqlite3 data/seller_platform.db "PRAGMA table_info(products);" | grep subject_id
```

Должна быть строка вида:
```
9|subject_id|INTEGER|0||0
```

## Откат миграции (если нужно)

Если нужно откатить миграцию (не рекомендуется):

```bash
sqlite3 data/seller_platform.db "ALTER TABLE products DROP COLUMN subject_id;"
```

Затем вернуться на предыдущий коммит:
```bash
git checkout <previous-commit>
```
