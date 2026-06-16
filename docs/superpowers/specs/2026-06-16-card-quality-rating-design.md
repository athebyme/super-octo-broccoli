# Дизайн фичи «Качество карточек» (Card Quality / WB Rating)

- Дата: 2026-06-16
- Статус: утверждён к реализации
- Связанные документы: [Фикс бага Андрея (3 фото)](2026-06-16-andrey-photos-fix-design.md)

## 1. Цель и контекст

Дать продавцу полноценный инструмент работы с качеством карточек, а не изолированное число рейтинга. Фича объединяет три источника сигнала и строит вокруг них обзор, детализацию, рекомендации и действия.

Постановка возникла из запроса «встроить оценку рейтинга карточек WB». При сверке с WB API выяснилось ключевое ограничение, определяющее весь дизайн.

### Ограничение WB API (подтверждено)

Выделенного эндпоинта «оценка качества карточки с разбивкой по фото/описанию/характеристикам и текстовыми рекомендациями» в публичном WB API **нет**. Детализация, видимая в кабинете продавца, через API не выгружается. Рейтинг доступен только как числовые поля внутри аналитических отчётов:

- `POST https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products`
  - тело: `selectedPeriod {start,end}` (формат `YYYY-MM-DD`, обязательно), опц. `nmIds` (uint64[], ≤1000), `brandNames`, `subjectIds`, `tagIds`, `skipDeletedNm`, `orderBy`, `limit` (≤1000, дефолт 50), `offset`;
  - ответ: `data.products[].product.{nmId, title, vendorCode, brandName, subjectId, subjectName, tags, productRating, feedbackRating, stocks}` + `.statistic`.
- Спека: `.skills/wb-seller-api-skill/swagger/11-analytics.yaml:28` (эндпоинт), `:4568` (ProductsRequest), `:5345` (DatePeriod), `:4809-4851` (productRating/feedbackRating).
- Лимит: 3 req/min (персональный/сервисный токен), 2 req/hour (basic). Для всего каталога — асинхронный отчёт `POST /api/v2/nm-report/downloads` → `GET /api/v2/nm-report/downloads/file/{downloadId}` (CSV с `IsRated/Rating/FeedbackRating`).

### Эмпирически подтверждённые шкалы (живой вызов 2026-06-16, 100 товаров)

| Поле | Шкала | Наблюдаемый разброс | Смысл |
|---|---|---|---|
| `productRating` | **0–10** (float, 1 знак) | `[6, 7, 7.3, 7.5, 7.8, 8, 9.3, 9.5, 9.7, 10]` | «Оценка карточки» глазами WB |
| `feedbackRating` | **0–5** (звёзды) | `[0, 1, 3, 3.3, 3.8, 4, 4.5, 4.8, 4.9, 5]`; `0` = нет отзывов | «Оценка по отзывам» |

Следствие: это **две разные шкалы**, объединять их в одно число нельзя. Комментарий `Product.nm_rating` «0–10» — верен (для `productRating`).

## 2. Концептуальная модель: две оси + AI-слой

Не смешиваем внешний рейтинг WB и нашу оценку — они отвечают на разные вопросы. Разрыв между ними сам по себе сигнал.

1. **WB-рейтинг (внешний, read-only).** `productRating` (0–10) + `feedbackRating` (0–5). Тянем из sales-funnel, показываем как факт + свежесть + тренд. Без разбивки — её WB не даёт.
2. **Quality Score (наш, 0–100, детерминированный, управляемый).** Композиция существующих сигналов; к каждому измерению привязана текстовая рекомендация «как поднять».
3. **AI-слой (по запросу).** Для низкорейтинговых карточек — прогон `card_doctor` (риск модерации) и `photo_optimizer` (качество/порядок фото) через существующий runner. Показываем как «мнение агентов», не подмешивая в число.

### 2.1 Расчёт Quality Score

Новый сервис `services/card_quality_scorer.py`:

```
compute_card_quality(card: dict) -> {
    score: float,            # 0-100
    status: 'excellent'|'good'|'average'|'poor',
    dimensions: {            # по каждому измерению
        <name>: {score: 0-100, status: 'ok'|'warning'|'error', weight: int, hint: str}
    },
    recommendations: [str],  # отсортированы по влиянию
}
```

- Источник проверок — переиспользуем логику `services/upload_readiness_validator.py` (`validate_product_upload_readiness(imported_product, seller=None)`, `:34`), которая уже даёт `checks{price, photos, characteristics, barcodes, brand, title, description, category}` со `status` и `issues[].{level, field, message, fix_hint}`. Рекомендации берём из `fix_hint`.
- Для **опубликованной** `Product` (а не `ImportedProduct`) — функция `_product_to_card_dict(product)` нормализует поля (`photos_json`, `characteristics_json`, `price`, `brand`, `title`, `description`, и т.д.) в общий вид. Решение «рефакторить `_check_*` под dict vs адаптер над `Product`» фиксируется на этапе плана; предпочтителен рефакторинг `_check_*` на приём нормализованного dict, чтобы и валидатор, и скорер ели один формат.
- Фото-измерение дополняем требованиями из `02-products.yaml`: ≤30 фото, мин. 700×900, наличие видео (бонус).
- `marketplace_validator.fill_percentage` (`marketplace_validator.py:121`) используем как суб-сигнал измерения «характеристики».

**Веса (сумма 100, настраиваемы константой):** characteristics 25, photos 20, description 15, title 10, brand 10, barcodes 10, price 5, category 5. Итог = взвешенная сумма суб-оценок 0–100.

**Статус-бэнды Quality Score:** ≥85 excellent, 70–84 good, 50–69 average, <50 poor.

### 2.2 Статус-бэнды WB-рейтинга (выровнены на реальные шкалы)

- `productRating` (0–10): ≥8 зелёный, 6–7.9 янтарный, <6 красный.
- `feedbackRating` (0–5): ≥4.5 зелёный, 3.5–4.4 янтарный, <3.5 красный; `0` — нейтрально-серый «нет отзывов».
- **Сопутствующее исправление калибровки:** в `templates/blocked_cards.html` пороги `>=4`/`>=3` трактуют `nm_rating` как 0–5, хотя шкала 0–10. Выравниваем на 0–10 во всех местах отображения `nm_rating` через общий Jinja-макрос (см. §6).

## 3. Модель данных и миграции

SQLite, без Alembic — идемпотентные скрипты (как в проекте).

- **`Product`** (`models.py:168`): `nm_rating` (Float, уже есть) ← `productRating`. Добавить:
  - `wb_feedback_rating` REAL ← `feedbackRating`;
  - `nm_rating_checked_at` DATETIME (свежесть WB-рейтинга);
  - `quality_score` REAL (наш 0–100);
  - `quality_breakdown_json` TEXT (сериализованный `dimensions`);
  - `quality_checked_at` DATETIME.
- **Новая таблица `card_rating_history`**: `id` PK, `product_id` FK→products.id, `nm_id` BIGINT, `wb_product_rating` REAL, `wb_feedback_rating` REAL, `quality_score` REAL, `captured_at` DATETIME; индекс `(product_id, captured_at)`. Питает тренды/спарклайны.
- Миграции:
  - `migrations/add_card_quality_columns.py` по образцу `migrations/add_nm_rating_column.py` (ALTER TABLE ... ADD COLUMN, идемпотентно через `add_column_if_missing`);
  - `migrations/migrate_add_card_rating_history.py` (CREATE TABLE IF NOT EXISTS, стиль `wb_orders` в `run_all_migrations.py`);
  - зарегистрировать обе в `migrations/run_all_migrations.py:migrate()`.
- Геттер `Product.get_quality_breakdown()` по конвенции `get_<field>()` (json.loads `*_json`).

## 4. Слой WB API и синхронизация

### 4.1 Метод клиента

В `services/wb_api_client.py` (рядом с Content/Analytics-методами, ~после `:2638`), на классе `WildberriesAPIClient`:

```
get_sales_funnel_products(nm_ids=None, period=None, brand_names=None,
                          subject_ids=None, limit=1000, offset=0,
                          log_to_db=False, seller_id=None) -> dict
    -> self._make_request('POST', 'analytics',
         '/api/analytics/v3/sales-funnel/products',
         json={...}, log_to_db=log_to_db, seller_id=seller_id)
```

- Хост `analytics` уже сконфигурирован (`ANALYTICS_API_URL`, `:104-108`, маппинг `_get_base_url:175`) — новых констант не нужно.
- Поле `nmId` в ответе — **с маленькой `d`** (не `nmID`).
- Проверять `result.get('error')` (WB иногда отдаёт 200 с `error:true`).
- Соблюдать 3 req/min: спейсинг 20с между батчами (прецедент `wb_api_client.py:2918`); встроенный `RateLimiter` (`:57`) не покрывает этот специфичный лимит.

Bulk-методы (для каталогов 100+): `request_nm_report_download(...)` и `poll_nm_report_file(download_id)` — асинхронный отчёт с колонками `Rating/FeedbackRating/IsRated`. Использовать когда товаров много, чтобы не упереться в 3 req/min.

### 4.2 Джоба планировщика

Новая функция `sync_card_ratings_all_sellers(flask_app)` в `services/product_sync_scheduler.py` по образцу `sync_blocked_cards_all_sellers` (`:353`):

- итерировать продавцов с валидным ключом (`Seller.has_valid_api_key()`);
- guard состояния (`last_sync_status='running'`) — новая `CardRatingSyncSettings` или переиспользование общей таблицы настроек синка;
- клиент: `WildberriesAPIClient(api_key=seller.wb_api_key, db_logger_callback=lambda **kwargs: APILog.log_request(**kwargs))`;
- собрать `nm_id` активных `Product` продавца, разбить на батчи ≤1000, вызвать `get_sales_funnel_products(nm_ids=batch, period=<последние ~30 дней>, log_to_db=True, seller_id=seller.id)`;
- из `data.products[].product` записать в `Product`: `nm_rating=productRating`, `wb_feedback_rating=feedbackRating`, `nm_rating_checked_at=utcnow()`; добавить строку `card_rating_history`;
- `quality_score` пересчитать детерминированно (дёшево) для активных карточек — в этой же джобе или отдельной;
- регистрация в планировщике рядом с инициализацией в `seller_platform.py:250` (интервал согласовать; sales-funnel обновляется WB почасово, разумно раз в несколько часов).

Замечание: и shadowed-cards (`nmRating`, `:525-532`), и sales-funnel (`productRating`) пишут в `Product.nm_rating` — это одна и та же 0–10 контент-оценка, конфликта значений нет; sales-funnel считаем авторитетным для активного каталога.

## 5. Сервис и роуты

`routes/card_quality.py`, регистрация в `seller_platform.py:6052-6164` (блюпринт по образцу `routes/marketplaces.py:14` или register-function по образцу `routes/blocked_cards.py:28`):

- `GET /card-quality` — страница-кокпит.
- `GET /api/card-quality/<product_id>` — JSON детали карточки (score, dimensions, recommendations, WB-рейтинг, тренд из истории).
- `POST /api/card-quality/<product_id>/ai-analyze` — постановка `AgentTask` (card_doctor + photo_optimizer) через runner; возвращает task id(s).
- `POST /api/card-quality/refresh` — синк по требованию для продавца/подмножества nm_id (через bulk или батч sales-funnel).

Сервисная логика чтения/агрегации — в `services/card_quality_scorer.py` + тонкий хелпер выборки/сортировки в роуте.

## 6. UI/UX

Дизайн-система `sh-*` (`templates/base.html`), Jinja + Tailwind CDN + Alpine.js, как `templates/analytics.html`.

### 6.1 Страница-кокпит `templates/card_quality.html`

- `sh-page-header` (заголовок Instrument Serif + подзаголовок + действия «Обновить рейтинги»).
- `stat_grid` из `stat_card` (`macros/components.html:52-79`): средний Quality Score, средний WB productRating, % карточек с productRating < порога, число «требуют внимания».
- Тренд-график (Chart.js, как в `analytics.html`) — средние WB-рейтинг и Quality Score по дням из `card_rating_history`.
- Таблица худших карточек (`data_table`, `:186`): миниатюра, vendor/nmId, WB productRating (бейдж 0–10), feedbackRating (звёзды 0–5), Quality Score (мини-gauge), статус, действия. Фильтры/сортировка по обеим осям и по поставщику (`filter_bar`).
- Клик по строке → `slideover` (`macros/overlays.html:245-300`) с деталью.

### 6.2 Новый компонент: круговой gauge

В системе кругового индикатора нет (только линейные бары `admin_supplier_parsing_quality.html:61-97`). Версаем новый: CSS `conic-gradient` ring или SVG `stroke-dasharray`, число в центре шрифтом `--font-display`, цвет по статус-палитре `#16a34a / #d97706 / #dc2626`. Параметризуется шкалой (0–100 для Quality Score, 0–10 для productRating). Оформить как Jinja-макрос `score_gauge(value, max, status)` в `macros/components.html` для переиспользования.

### 6.3 Детальный slideover

```
┌─ Качество карточки · Артикул 12345678 ───────────────────────┐
│      ╭─────────╮     WB: оценка карточки  8.0 / 10  (3ч)     │
│      │   78    │     WB: по отзывам       4.8 ★  (0–5)       │
│      │ /100    │     ▁▂▃▅▆ тренд 30 дней                     │
│      ╰─────────╯                                             │
│      Quality Score                                           │
│  ──────────────────────────────────────────────────────────  │
│  Фото            ███████░░░  70%   ⚠ 3 фото — мало           │
│  Характеристики  █████████░  90%   ✓                         │
│  Описание        ██████░░░░  60%   ⚠ короткое                │
│  Бренд/штрихкоды ██████████ 100%   ✓                         │
│  ──────────────────────────────────────────────────────────  │
│  Рекомендации (alert_box): + добавить фото (до 30) · ...     │
│  [ 🤖 Глубокий AI-анализ ]  [ Открыть карточку ]             │
└──────────────────────────────────────────────────────────────┘
```

- Суб-оценки — линейные бары (идиома `admin_supplier_parsing_quality.html`).
- Рекомендации — `alert_box` (`components.html:169`), тип по `status`.
- Async-действия — тосты `$store.toasts` (`base.html:2419`).

### 6.4 Виджеты на существующих листингах

- Колонка WB-рейтинга + Quality Score в `templates/cards.html` (сейчас рейтинга нет) и в листинге «Мои товары».
- Общий Jinja-макрос `wb_rating_badge(value)` (0–10, корректные пороги) — заменяет хардкод порогов в `blocked_cards.html:380-405` (фикс калибровки §2.2).

### 6.5 Навигация

Пункт sidebar в seller-секции `base.html` (`:1820+`) + опц. пункт command palette (`:2429-2480`). Навигация хардкодом — правим `base.html` напрямую.

## 7. AI-агенты (по запросу)

- Кнопка «Глубокий AI-анализ» ставит `AgentTask` через существующий runner (`agents/runner.py:88`) / orchestrator: `card_doctor` (`diagnose_single`) + `photo_optimizer`.
- Результат (risk_score 0–10, issues, recommendations, recommended_order фото) подгружается в slideover по готовности (поллинг статуса задачи, как в существующем UI агентов).
- Авто-подсказка запустить агентов для карточек ниже порога Quality Score / productRating.
- Каталожная запись агента уже есть (`agent_service.py:160-175`); переиспользуем, не дублируем.

## 8. Ошибки, лимиты, безопасность

- WB 200 + `error:true` → проверяем явно; маппинг исключений `WBAPIException/WBAuthException/WBRateLimitException` (`wb_api_client.py:42-53`).
- Лимит 3 req/min sales-funnel → батч ≤1000 nmId + спейсинг 20с; для bulk — async nm-report.
- Токен — чувствительный: использовался однократно для верификации шкалы (не сохранён, не залогирован). В рантайме рейтинг тянется фоновой джобой по уже зашифрованному `seller.wb_api_key` (Fernet, `models.py:103-141`) — сырой токен в код/конфиг не попадает.
- Отображение устойчиво к отсутствию данных: `nm_rating IS NULL` → «нет данных», `feedbackRating == 0` → «нет отзывов».

## 9. Тестирование и верификация

- Юнит-тесты: `card_quality_scorer.compute_card_quality` (детерминированность, веса, бэнды, граничные случаи пустых полей); парсинг ответа sales-funnel (мок `_make_request`); идемпотентность миграций (повторный прогон не падает); рендер `score_gauge`/`wb_rating_badge` (корректные пороги 0–10).
- Интеграция: джоба `sync_card_ratings_all_sellers` на мок-клиенте (батчинг, спейсинг, запись истории).
- Ручная верификация: после первой реальной выгрузки сверить, что gauge productRating корректно отображает 0–10, feedbackRating 0–5; прогнать `/run` приложения и проверить страницу/slideover визуально.

## 10. Очерёдность реализации (для плана)

1. Данные: миграции (`Product` колонки + `card_rating_history`) + модели.
2. WB API: `get_sales_funnel_products` + bulk-методы + тесты на моках.
3. Скорер: `card_quality_scorer.py` + рефактор/адаптер валидатора + тесты.
4. Синк: джоба `sync_card_ratings_all_sellers` + регистрация в планировщике.
5. Роуты + сервисная выборка.
6. UI: gauge/badge макросы, кокпит-страница, slideover, виджеты на листингах, навигация, фикс калибровки `blocked_cards.html`.
7. AI-слой: кнопка и интеграция с runner.
8. Верификация: реальная выгрузка, визуальная проверка, прогон тестов.

## 11. Открытые/настраиваемые параметры (значения по умолчанию приняты)

- Веса Quality Score и статус-бэнды — вынесены в константы, можно тюнить без переписывания.
- Интервал джобы синка — согласовать (рекоменд. раз в несколько часов; WB обновляет рейтинг почасово).
- Порог «требуют внимания» для авто-подсказки AI — дефолт productRating < 6 или Quality Score < 50.

## 12. Вне объёма (YAGNI)

- Не воспроизводим внутреннюю рубрику WB (API её не отдаёт).
- Не агрегируем LLM-оценки агентов в число Quality Score (async, не всегда присутствуют).
- Не трогаем легаси HTTP-слои `services/wildberries_api.py` и `services/wb_data_sync.py` — новые вызовы только через `WildberriesAPIClient`.
