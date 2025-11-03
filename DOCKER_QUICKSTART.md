# Docker Quick Start Guide

## Быстрый старт с Docker Compose

### 1. Подготовка окружения

```bash
# Клонировать репозиторий (если еще не сделано)
git clone <repo-url>
cd super-octo-broccoli

# Создать .env файл
cp .env.docker .env

# Сгенерировать ключ шифрования
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Скопируйте вывод и вставьте в .env как ENCRYPTION_KEY=...
```

### 2. Настройка .env файла

Отредактируйте `.env`:

```env
# Seller Platform
SELLER_PORT=5001
SECRET_KEY=измените-этот-ключ-на-свой-секретный

# КРИТИЧНО: Вставьте сгенерированный ключ шифрования
ENCRYPTION_KEY=ваш-сгенерированный-ключ-fernet

# WB Calculator
CALCULATOR_PORT=5000
APP_SECRET_KEY=wb-calculator-secret-измените
```

### 3. Запуск приложений

```bash
# Собрать образы
docker-compose build

# Запустить оба приложения
docker-compose up -d

# Или запустить только одно
docker-compose up -d seller-platform  # Платформа продавцов
docker-compose up -d wb-calculator    # Калькулятор прибыли
```

### 4. Проверка работы

```bash
# Проверить статус контейнеров
docker-compose ps

# Проверить логи
docker-compose logs seller-platform
docker-compose logs wb-calculator

# Следить за логами в реальном времени
docker-compose logs -f seller-platform
```

### 5. Доступ к приложениям

- **Seller Platform:** http://localhost:5001
- **WB Calculator:** http://localhost:5000

### 6. Первый запуск - создание админа

```bash
# Войти в контейнер
docker exec -it seller-platform sh

# Создать администратора
python - <<'EOF'
from seller_platform import app, db
from models import User

with app.app_context():
    # Проверить, есть ли уже админ
    admin = User.query.filter_by(is_admin=True).first()
    if admin:
        print(f"Админ уже существует: {admin.username}")
    else:
        # Создать нового админа
        admin = User(
            username="admin",
            email="admin@example.com",
            is_admin=True,
            is_active=True
        )
        admin.set_password("admin123")  # ИЗМЕНИТЕ ПАРОЛЬ!
        db.session.add(admin)
        db.session.commit()
        print(f"Админ создан: {admin.username}")
        print("⚠️  ВАЖНО: Смените пароль после первого входа!")
EOF

# Выйти из контейнера
exit
```

### 7. Вход в систему

1. Откройте http://localhost:5001/login
2. Войдите как:
   - **Username:** admin
   - **Password:** admin123 (или тот, что вы установили)
3. **Сразу смените пароль!**

## Управление Docker

### Остановка приложений

```bash
# Остановить все
docker-compose down

# Остановить одно приложение
docker-compose stop seller-platform
docker-compose stop wb-calculator
```

### Перезапуск

```bash
# Перезапустить все
docker-compose restart

# Перезапустить одно
docker-compose restart seller-platform
```

### Обновление кода

```bash
# 1. Подтянуть изменения
git pull

# 2. Пересобрать образы
docker-compose build

# 3. Перезапустить
docker-compose up -d
```

### Просмотр логов

```bash
# Последние 100 строк
docker-compose logs --tail=100 seller-platform

# Следить в реальном времени
docker-compose logs -f seller-platform

# Логи всех сервисов
docker-compose logs -f
```

### Выполнение команд внутри контейнера

```bash
# Открыть shell
docker exec -it seller-platform sh

# Выполнить Python скрипт
docker exec seller-platform python migrate_db.py --db-path /app/data/seller_platform.db

# Проверить БД
docker exec seller-platform sqlite3 /app/data/seller_platform.db ".tables"
```

## Данные и volumes

### Расположение данных

Данные хранятся в директориях на хосте:

```
./uploads/     - Загруженные файлы
./processed/   - Обработанные отчеты
./data/        - База данных SQLite
```

### Резервное копирование

```bash
# Создать резервную копию БД
cp data/seller_platform.db data/backups/seller_platform_$(date +%Y%m%d_%H%M%S).db

# Или через Docker
docker exec seller-platform cp /app/data/seller_platform.db /app/data/seller_platform.backup
```

### Очистка данных

```bash
# ОСТОРОЖНО: Удалит все данные!
docker-compose down -v  # Удалить volumes
rm -rf data/ uploads/ processed/
docker-compose up -d
```

## Миграция базы данных

При обновлении кода может потребоваться миграция БД. Она выполняется **автоматически** при запуске.

Подробнее: [MIGRATION_GUIDE.md](./MIGRATION_GUIDE.md)

### Проверка миграции

```bash
# Посмотреть логи миграции
docker-compose logs seller-platform | grep -A 20 "миграци"

# Проверить структуру БД
docker exec seller-platform sqlite3 /app/data/seller_platform.db ".schema sellers"
```

### Ручная миграция

Если автоматическая миграция не сработала:

```bash
# Остановить контейнер
docker-compose stop seller-platform

# Запустить миграцию вручную
docker-compose run --rm seller-platform python migrate_db.py --db-path /app/data/seller_platform.db

# Запустить контейнер
docker-compose start seller-platform
```

## Мониторинг

### Использование ресурсов

```bash
# Статистика контейнеров
docker stats seller-platform wb-calculator

# Детали контейнера
docker inspect seller-platform
```

### Проверка здоровья

```bash
# Проверить доступность
curl http://localhost:5001/login
curl http://localhost:5000/

# Проверить статус
docker-compose ps
```

## Troubleshooting

### Контейнер не запускается

```bash
# Проверить логи
docker-compose logs seller-platform

# Проверить ошибки сборки
docker-compose build --no-cache seller-platform

# Проверить порты
netstat -tulpn | grep 5001
```

### База данных заблокирована

```bash
# Остановить все
docker-compose down

# Удалить lock файлы
rm data/*.db-shm data/*.db-wal

# Запустить снова
docker-compose up -d
```

### Ошибки миграции

См. [MIGRATION_GUIDE.md](./MIGRATION_GUIDE.md#troubleshooting)

### Ошибки при шифровании API ключей

```bash
# Проверить наличие ENCRYPTION_KEY
docker exec seller-platform env | grep ENCRYPTION_KEY

# Если нет, добавить в .env и перезапустить
echo "ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" >> .env
docker-compose restart seller-platform
```

## Production рекомендации

### Безопасность

1. **Изменить все секретные ключи** в .env
2. **Использовать SSL/TLS** через reverse proxy (nginx)
3. **Ограничить доступ** к портам через firewall
4. **Регулярные бэкапы** базы данных

### Reverse Proxy (nginx)

Пример конфигурации nginx:

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://localhost:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Логирование

```bash
# Ротация логов
docker run -d \
  --name=seller-platform \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  ...
```

### Мониторинг

Рекомендуемые инструменты:
- **Portainer** - веб-интерфейс для Docker
- **Prometheus + Grafana** - метрики
- **Loki** - централизованное логирование

## Полезные команды

```bash
# Просмотр образов
docker images | grep seller

# Очистка неиспользуемых образов
docker image prune -a

# Экспорт/импорт контейнера
docker export seller-platform > seller-platform.tar
docker import seller-platform.tar

# Копирование файлов
docker cp seller-platform:/app/data/seller_platform.db ./backup.db
docker cp ./file.txt seller-platform:/app/uploads/

# Просмотр процессов в контейнере
docker top seller-platform
```

## Дополнительная информация

- **API документация:** [WB_API_SETUP.md](./WB_API_SETUP.md)
- **Миграция БД:** [MIGRATION_GUIDE.md](./MIGRATION_GUIDE.md)
- **Основной README:** [README.md](./README.md)

---

**Версия:** 1.0.0
**Дата:** 2025-11-03
