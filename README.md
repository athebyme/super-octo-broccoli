# WB Seller Platform

Платформа управления товарами и продажами на Wildberries: авто-импорт товаров от поставщиков, умное ценообразование, AI-обогащение карточек, мониторинг цен, объединение дублей, и калькулятор прибыли.

## Возможности

| Модуль | Что делает |
|--------|-----------|
| **Авто-импорт** | Парсит каталоги поставщиков (Sexoptovik и др.), импортирует товары на WB с AI-классификацией категорий |
| **Ценообразование** | Автоматический расчёт цен на основе закупочной стоимости, логистики и желаемой маржи |
| **AI-обогащение** | GPT/GigaChat генерирует заголовки, описания, характеристики для карточек WB |
| **Генерация изображений** | AI создаёт инфографику и фото для товарных карточек |
| **Мониторинг цен** | Безопасное изменение цен с откатом, защита от демпинга |
| **Объединение карточек** | Поиск дублей и рекомендации по мержу карточек WB |
| **Блокировки** | Отслеживание заблокированных и затенённых карточек |
| **База поставщиков** | Управление каталогом поставщиков с обогащением данных |
| **Калькулятор** | Расчёт прибыли по отчётам WB с учётом логистики и упаковки |
| **Админ-панель** | Управление продавцами, пользователями и настройками |

## Быстрый старт

### Docker (рекомендуется)

```bash
# 1. Запуск
docker compose up -d --build

# 2. Создание админа
docker compose exec seller-platform flask --app seller_platform create_admin

# 3. Открыть http://localhost:5001/login
```

### Локально

```bash
# 1. Виртуальное окружение
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 2. Зависимости
pip install -r requirements.txt

# 3. Инициализация БД и админа
python scripts/init_platform.py

# 4. Запуск
python seller_platform.py     # → http://localhost:5001
```

Калькулятор прибыли запускается отдельно: `python app.py` → http://localhost:5000

## Стек

- **Backend:** Flask, Flask-Login, Flask-SQLAlchemy, SQLite, Gunicorn
- **Frontend:** TailwindCSS (CDN), Alpine.js, Jinja2-шаблоны
- **AI:** OpenAI / GigaChat API для обогащения карточек и генерации изображений
- **Интеграции:** Wildberries Content API, Sexoptovik API
- **Планировщик:** APScheduler — синхронизация товаров, мониторинг блокировок

## Структура проекта

```
├── seller_platform.py          # Точка входа: Flask-приложение, маршруты
├── app.py                      # Калькулятор прибыли (отдельное приложение)
├── models.py                   # SQLAlchemy-модели (User, Seller, Product, ...)
│
├── routes/                     # Маршруты (Flask Blueprints)
│   ├── auto_import.py          #   Авто-импорт товаров
│   ├── enrichment.py           #   AI-обогащение карточек
│   ├── safe_prices.py          #   Мониторинг и безопасное изменение цен
│   ├── merge_cards.py          #   Объединение дублей карточек
│   ├── blocked_cards.py        #   Блокировки и затенения
│   └── suppliers.py            #   Управление поставщиками
│
├── services/                   # Бизнес-логика
│   ├── ai_service.py           #   GPT/GigaChat — генерация контента
│   ├── wb_api_client.py        #   Клиент Wildberries Content API
│   ├── auto_import_manager.py  #   Менеджер авто-импорта
│   ├── pricing_engine.py       #   Расчёт цен
│   ├── supplier_service.py     #   Сервис поставщиков
│   ├── supplier_enrichment.py  #   Обогащение данных поставщика
│   ├── image_generation_service.py  #  Генерация изображений
│   ├── merge_recommendations.py     #  Рекомендации по объединению
│   ├── product_sync_scheduler.py    #  Фоновая синхронизация
│   ├── photo_cache.py          #   Кэш фотографий
│   ├── brand_cache.py          #   Кэш брендов WB
│   ├── wb_product_importer.py  #   Импорт товаров на WB
│   ├── wb_categories_mapping.py #  Маппинг категорий WB
│   ├── wb_validators.py        #   Валидация данных для WB
│   ├── data_export.py          #   Экспорт данных
│   └── wildberries_api.py      #   Legacy WB API обёртка
│
├── templates/                  # Jinja2-шаблоны (~54 файла)
├── static/                     # CSS, JS, изображения
│
├── migrations/                 # Скрипты миграции БД
├── scripts/                    # Утилиты: init, backup, диагностика
├── docs/                       # Документация, гайды
│   └── api_specs/              #   OpenAPI-спецификации WB API
├── data/                       # CSV, SQL, тестовые данные
│
├── Dockerfile                  # Docker-образ
├── docker-compose.yml          # Оркестрация контейнеров
├── docker-entrypoint.sh        # Скрипт запуска (миграции + gunicorn)
└── requirements.txt            # Python-зависимости
```

## Настройка

### Переменные окружения

Скопируйте `.env.example` → `.env` и заполните:

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `SECRET_KEY` | Ключ сессий Flask | auto-generated |
| `DATABASE_URL` | Путь к SQLite БД | `sqlite:///seller_platform.db` |
| `SELLER_PORT` | Порт платформы | `5001` |
| `CALCULATOR_PORT` | Порт калькулятора | `5000` |

AI-ключи и WB API-ключи настраиваются через интерфейс администратора в разделе настроек продавца.

## Production-деплой

```bash
# Gunicorn
gunicorn -w 4 -b 0.0.0.0:5001 seller_platform:app

# Или через Docker
docker compose up -d --build
```

Для systemd и nginx см. [docs/PLATFORM_README.md](docs/PLATFORM_README.md).

## Документация

| Документ | Описание |
|----------|----------|
| [PLATFORM_README](docs/PLATFORM_README.md) | Полное руководство по платформе |
| [DOCKER_QUICKSTART](docs/DOCKER_QUICKSTART.md) | Docker-деплой |
| [AUTO_IMPORT_README](docs/AUTO_IMPORT_README.md) | Авто-импорт товаров |
| [WB_API_SETUP](docs/WB_API_SETUP.md) | Настройка WB API |
| [MIGRATION_GUIDE](docs/MIGRATION_GUIDE.md) | Миграции БД |
| [ADMIN_CREDENTIALS](docs/ADMIN_CREDENTIALS.md) | Учётные данные |

## Лицензия

Проект для внутреннего использования.
