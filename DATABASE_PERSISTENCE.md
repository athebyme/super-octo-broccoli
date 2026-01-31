# 🗄️ Сохранность базы данных

## Проблема и решение

**Проблема:** База данных стиралась при выполнении `docker-compose up -d --build`

**Корневая причина:**
- Использовался bind mount `./data:/app/data`
- При пересборке Docker мог создавать конфликты с локальной директорией
- Права доступа root:root затрудняли работу

**Решение:**
- Переход на Docker Named Volume: `seller_platform_data`
- Named volumes управляются Docker и сохраняются между пересборками
- Автоматическое управление правами доступа

## Текущая конфигурация

База данных теперь хранится в **Docker Named Volume**: `seller_platform_data`

Преимущества:
- ✅ Сохраняется при `docker-compose down`
- ✅ Сохраняется при `docker-compose up -d --build`
- ✅ Сохраняется при пересборке образа
- ✅ Автоматические права доступа
- ✅ Изолирован от хост-системы

## Управление данными

### Создание бэкапа

```bash
./backup_database.sh
```

Бэкап будет сохранен в `./backups/seller_platform_YYYYMMDD_HHMMSS.db`

### Восстановление из бэкапа

```bash
./restore_database.sh backups/seller_platform_20260122_123456.db
```

### Ручной доступ к базе (для администраторов)

Просмотр базы данных напрямую:

```bash
# Запустить sqlite3 в контейнере
docker compose exec seller-platform sqlite3 /app/data/seller_platform.db

# Примеры команд:
.tables                          # показать все таблицы
.schema users                    # структура таблицы
SELECT * FROM users LIMIT 10;   # выборка данных
.exit                           # выход
```

Копирование базы на хост (для анализа):

```bash
docker run --rm \
  -v super-octo-broccoli_seller_platform_data:/data \
  -v "$(pwd):/backup" \
  alpine \
  cp /data/seller_platform.db /backup/database_copy.db
```

### Очистка данных (ОСТОРОЖНО!)

Удаление volume с базой данных:

```bash
# ВНИМАНИЕ: Это удалит ВСЕ данные безвозвратно!
docker compose down
docker volume rm super-octo-broccoli_seller_platform_data
docker compose up -d  # создаст новую чистую базу
```

## Безопасное обновление

Рекомендуемая последовательность при обновлении:

```bash
# 1. Создать бэкап
./backup_database.sh

# 2. Остановить контейнеры
docker compose down

# 3. Обновить код
git pull

# 4. Пересобрать и запустить
docker compose up -d --build

# 5. Проверить что все работает
docker compose logs seller-platform

# 6. Если что-то не так, восстановить из бэкапа
# ./restore_database.sh backups/seller_platform_YYYYMMDD_HHMMSS.db
```

## Где физически хранится volume?

Docker хранит named volumes в системной директории:

```bash
# Linux: /var/lib/docker/volumes/super-octo-broccoli_seller_platform_data/_data/
# Mac: ~/Library/Containers/com.docker.docker/Data/vms/0/
# Windows: \\wsl$\docker-desktop-data\data\docker\volumes\

# Просмотр информации о volume:
docker volume inspect super-octo-broccoli_seller_platform_data
```

## Миграция с bind mount на named volume

Если у вас уже была база в `./data/seller_platform.db`:

```bash
# 1. Остановить контейнеры
docker compose down

# 2. Создать volume (если не существует)
docker volume create super-octo-broccoli_seller_platform_data

# 3. Скопировать существующую базу в volume
docker run --rm \
  -v "$(pwd)/data:/source" \
  -v super-octo-broccoli_seller_platform_data:/target \
  alpine \
  cp /source/seller_platform.db /target/seller_platform.db

# 4. Запустить с новой конфигурацией
docker compose up -d

# 5. Проверить
docker compose logs seller-platform | grep "Используется база данных"
```

## Troubleshooting

### База данных пустая после запуска

Проверьте, что volume существует:
```bash
docker volume ls | grep seller_platform_data
```

Проверьте файлы в volume:
```bash
docker run --rm -v super-octo-broccoli_seller_platform_data:/data alpine ls -lh /data/
```

### Ошибка "database is locked"

Это исправлено в последней версии (WAL mode + retry логика), но если возникает:
```bash
# Перезапустить контейнер
docker compose restart seller-platform
```

### Нужно вернуться к ./data (bind mount)

Не рекомендуется, но если необходимо:

1. Сделайте бэкап текущих данных
2. Измените в `docker-compose.yml`:
   ```yaml
   volumes:
     - ./data:/app/data  # вместо seller_platform_data:/app/data
   ```
3. Восстановите бэкап в `./data/seller_platform.db`
4. `docker compose up -d`

## Автоматические бэкапы (рекомендуется)

Добавьте в crontab для автоматических бэкапов:

```bash
# Ежедневный бэкап в 3:00 AM
0 3 * * * cd /path/to/super-octo-broccoli && ./backup_database.sh

# Очистка старых бэкапов (старше 30 дней)
0 4 * * * find /path/to/super-octo-broccoli/backups -name "*.db" -mtime +30 -delete
```
