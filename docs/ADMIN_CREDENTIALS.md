# Учетные данные администратора

## По умолчанию

При первом запуске автоматически создается администратор со следующими учетными данными:

```
Логин:  admin
Пароль: admin123
Email:  admin@example.com
```

## Вход в систему

1. Откройте в браузере: `http://localhost:5001`
2. Используйте указанные выше учетные данные
3. **ВАЖНО**: Смените пароль после первого входа!

## Изменение дефолтных учетных данных

Вы можете задать свои учетные данные через переменные окружения в `docker-compose.yml`:

```yaml
environment:
  - ADMIN_USERNAME=your_username
  - ADMIN_EMAIL=your_email@example.com
  - ADMIN_PASSWORD=your_secure_password
```

Или создать файл `.env` в корне проекта:

```bash
ADMIN_USERNAME=your_username
ADMIN_EMAIL=your_email@example.com
ADMIN_PASSWORD=your_secure_password
```

## Создание нового администратора вручную

Если нужно создать еще одного администратора:

```bash
docker-compose exec seller-platform python init_platform.py
```

Скрипт запросит данные нового администратора в интерактивном режиме.

## Сброс пароля

Если забыли пароль:

1. Остановите контейнер: `docker-compose down`
2. Удалите БД: `rm -f data/seller_platform.db`
3. Запустите заново: `docker-compose up -d`
4. Администратор будет создан с дефолтными учетными данными
