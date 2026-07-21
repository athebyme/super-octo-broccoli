# Мониторинг конкурентов v2 — дизайн

Дата: 2026-07-21. Статус: одобрен пользователем (вариант «полный рефактор», приоритет — стабильность).

## 1. Цель

Надёжный «радар» цен/остатков/рейтингов конкурентов WB со связкой с собственными
карточками продавца: позиция по цене внутри группы, история, алерты в общем
центре уведомлений. Без автоматического изменения собственных цен. Масштаб: до
~300 товаров на продавца (server-side cap 1000), интервал обновления
настраиваемый 30–1440 минут, default 60.

## 2. Диагноз v1 (зафиксированные факты)

- **Архитектура тредов.** Per-seller daemon-треды стартуют и из web-воркеров
  (включение в UI), и из scheduler-процесса: при 2 gunicorn workers + singleton
  scheduler возможны дублирующиеся циклы в трёх процессах. Дедлок
  `check_and_restart_monitor_loops` висел 4 месяца (fix b579e77) — мониторинг
  тихо умирал после падения треда.
- **Снимки-мусор.** Прод: 9 961 337 строк `competitor_price_snapshots`
  (~460 МБ) на **2 отслеживаемых товара**; 1 639 снимков/сутки. Причина:
  fetch-miss записывается как «изменение на None», следующий успех — снова
  «изменение». Суточная компакция (`DELETE … NOT IN (SELECT MAX(id)…)` по
  10M-таблице) не справляется и держит SQLite write-lock.
- **Данные не подтягиваются.** Цены добываются цепочкой basket→каталог
  продавца→search по бренду; для 18+ товаров (у пилотного продавца именно они,
  `is_adult=true` в card.json) публичная выдача фильтруется — у одного из двух
  товаров цена не находилась никогда. Miss при этом затирает last-known цену.
- **Сожжённый IP-бюджет.** Цикл с паузой 60с генерирует запросы непрерывно;
  живые пробы с прод-хоста: `search.wb.ru` и `catalog.wb.ru` отдают 429
  практически мгновенно. `card.wb.ru` (v1/v2 detail) мёртв — 404. basket CDN
  (`wbbasket.ru`) работает без лимитов.
- **Опасные HTTP-пути.** `POST /api/competitors/products` синхронно ходит в WB
  с `time.sleep(30–60)` при 429 и постраничным обходом — тот же класс проблемы,
  что инцидент фото-прокси 2026-07-20.
- **Изоляция от платформы.** `CompetitorAlert` не виден в общем колокольчике;
  график на захардкоженных hex вне chart-токенов; `proxy_url` с
  логином/паролем — плейнтекст в БД и отдаётся в `to_dict()`.
- **Ложная семантика.** Комментарии моделей обещают «цены в копейках»,
  фактически хранятся рубли (`… // 100` при парсинге). Тестов на роуты, парсинг
  WB и алерты нет.

## 3. Архитектура сбора (замена тредов)

Треды удаляются полностью (`start/stop_competitor_monitor_loop`,
`check_and_restart_monitor_loops`, `_monitor_threads`, `_stop_events`,
`is_running`-семафор через БД). Сбор — bounded-джоб singleton scheduler:

- `competitor_monitor_tick` в `services/product_sync_scheduler.py`:
  `IntervalTrigger(minutes=1)`, `max_instances=1`, `coalesce=True`. За тик — до
  **2 продавцов** с `is_enabled=1` и (`next_sync_due_at IS NULL OR
  next_sync_due_at <= now`), oldest-first.
- По завершении sync: `next_sync_due_at = now + sync_interval_minutes`,
  статистика в settings. «Синхронизировать сейчас» из UI просто ставит
  `next_sync_due_at = now` (ответ мгновенный, запуск в течение минуты) — web
  больше вообще не ходит в WB для синка.
- Sync фазируется по инварианту SQLite: сначала вся сеть (fetch всех батчей
  без открытой write-транзакции), затем один проход записи с `begin_nested()`
  на товар и общим commit. Wall-clock бюджет sync одного продавца ~90с;
  не уложились — честный partial, хвост в следующем тике (сортировка
  `last_fetched_at nullsfirst` уже это обеспечивает).
- `is_running`/`last_sync_status='running'` выставляется джобом на время
  выполнения (для UI), сбрасывается в finally.

## 4. Fetch-слой (переписанный `CompetitorMonitorService`)

Источники и порядок:

1. **Метаданные** — basket CDN `card.json` + `sellers.json` через существующий
   резолвер `services/wb_media.py`. Только для товаров без метаданных или со
   `metadata_synced_at` старше 7 дней — не каждый цикл. Сохраняем также
   `is_adult` и предмет. 404 от basket = реальный сигнал «товар удалён»
   (инкремент `fetch_error_count`, деактивация после 20 подряд).
2. **Цены/остатки/рейтинг** — группировка товаров по `wb_supplier_id` →
   `catalog.wb.ru/sellers/catalog` (до 5 страниц на продавца за sync);
   остаток — `search.wb.ru/exactmatch/…/v18/search` по бренду (до 2 страниц
   на бренд). Парсер единый: `sizes[0].price.basic/product` (копейки → рубли),
   fallback `priceU/salePriceU`.

Правила устойчивости:

- **Один глобальный process-wide RateLimiter** на все публичные WB-вызовы всех
  продавцов (бюджет per-IP, не per-seller): default 20 rpm, env
  `COMPETITOR_PUBLIC_RPM`. Настройка `requests_per_minute` из seller-UI
  удаляется (колонка остаётся, игнорируется).
- **Circuit breaker per источник** (in-memory, scheduler singleton): 3
  последовательных 429/5xx → cooldown 10 минут. 429 немедленно завершает
  источник в текущем sync — никаких `time.sleep` ожиданий.
- Кросс-селлер кэш результатов остаётся (TTL 300с) с ограничением размера.
- Прокси: опциональный per-seller, шифруется (см. §7).
- `FEEDBACKS_URL` и fallback `_fetch_seller_via_search` удаляются (мертвы/
  избыточны).

## 5. Политика наблюдений и снимков

- **Fetch-miss ≠ наблюдение.** Если источник не вернул товар: `current_*` НЕ
  затираются, снимок НЕ пишется; `price_miss_count += 1`. UI показывает
  честную свежесть («цена от <last_price_at>»). Miss цены не считается ошибкой
  товара и не ведёт к деактивации.
- **Снимок** — только при успешном наблюдении и фактическом изменении
  (price, sale_price, total_stock, rating) либо первом наблюдении.
  `last_price_at` обновляется при каждом успешном наблюдении цены.
- Семантика цен фиксируется: **рубли, integer** (комментарии моделей
  исправляются под фактические данные).
- **Компакция переписывается**: кандидаты выбираются read-only, удаление
  чанками по 5 000 строк с отдельными commit (без длинного write-lock).
  Дополнительно: дедуп подряд идущих одинаковых снимков, удаление
  всех-NULL снимков, ретеншн прочитанных алертов 90 дней.
- **Одноразовая чистка прод-мусора** — идемпотентная миграция
  `migrate_compact_competitor_snapshots.py`: чанковое удаление all-NULL и
  подряд-дубликатов с бюджетом времени на прогон (~60с за старт entrypoint);
  недочищенный хвост добирает переписанная регулярная компакция. Возврат места
  на диске — отдельный ручной VACUUM (в раннбук, не в миграцию).

## 6. Роуты и UX-потоки

- `POST /api/competitors/products` (nm_ids): только строгая валидация
  (уникальные positive int, без coercion bool/float/строк, cap 300) и вставка
  строк с одними nm_id; метаданные/цены придут от sync (`next_sync_due_at =
  now`). Ноль вызовов WB в запросе. Новые строки в UI — состояние
  «ожидает данных».
- Интерактивный поиск (`/api/competitors/search`) и превью каталога продавца
  (`/api/competitors/seller-catalog`): остаются синхронными, но строго
  bounded — 1 запрос/1 страница, timeout 10с, при 429 — честный ответ 503
  «WB ограничивает запросы, повторите позже». Никаких sleep/пагинации в
  request path.
- Импорт каталога продавца целиком: группа получает `auto_source='seller'` +
  `import_requested=1`; постраничный fetch выполняет scheduler внутри sync
  (до 3 страниц за тик, до 300 товаров суммарно), по завершении флаг
  снимается. Прогресс виден по числу товаров в группе.
- Настройки: `sync_interval_minutes` (30–1440), порог цены % (существующий),
  порог скидки в п.п. (новый `discount_alert_pp`, default 5), `max_products`
  cap 1000 server-side, прокси (маскированный). `pause_between_cycles_seconds`
  и `requests_per_minute` из UI удаляются.

## 7. Прокси как credential

`proxy_url` может содержать `user:pass` ⇒ обращается с ним как с credential:
модель получает property поверх колонки — чтение поддерживает legacy plaintext
(как `Seller.wb_api_key`), запись нового значения fail-closed шифрует Fernet
(`ENCRYPTION_KEY` обязателен). `to_dict()`/UI отдают только маску
`scheme://host:port` + флаг «заданы логин/пароль»; полное значение наружу не
возвращается никогда.

## 8. Алерты и уведомления

- Генерация — как v1 (price_drop/price_increase, discount ±5 п.п.,
  out_of_stock/back_in_stock), но исключительно на основе успешных наблюдений.
- **Зеркалирование в общий центр**: по завершении sync с новыми алертами
  создаётся одно агрегированное `Notification` через `create_notification()`
  (`category`: critical→error, warning→warning, иначе info; title
  «Конкуренты: изменения», message вида «3 изменения цены, 1 товар закончился»,
  link `/competitors/alerts`) с дедупом 4 часа по title — по образцу
  `notify_supplier_updates`.
- `CompetitorAlert` остаётся детальным журналом на своей странице.

## 9. Связка со своими товарами

- Группа привязывается к своей карточке (`own_product_id`, уже есть): в UI —
  выбор из своих `Product` (поиск), на детали группы — закреплённая строка
  «Ваш товар» (цена из `Product.discount_price`/`price`), позиция в ценовом
  ряду («2-й из 8 по цене»), отклонение от минимума и медианы.
- Дашборд: карточка группы показывает позицию own vs конкуренты и бейдж
  «дороже минимума на X%» для групп, требующих внимания.
- На графике истории конкурента — горизонтальная референс-линия текущей своей
  цены (если группа привязана).

## 10. UI («Тёплая редакция»)

- Все 5 страниц переводятся на sh-* компоненты и токены: иконки из
  `macros/icons.html`, статусные цвета через `var(--danger/-ok/-warn/-info…)`,
  без inline-hex; focus/active states; обе темы; empty/loading/error states.
- График истории: Chart.js 4.4.0 (CDN per-page, как analytics) +
  `window.shChart.register` и палитра `--chart-N`; референс-линия своей цены.
- Поллинг дашборда — только с visibility-гейтом (`document.hidden`), как
  сторы `tasks`/`notif`.
- Пункт сайдбара сохраняется без изменений структуры.

## 11. Модели и миграции

`migrations/migrate_competitor_monitor_v2.py` (идемпотентная, fail-fast в
entrypoint + `run_all_migrations.py`):

- `competitor_monitor_settings`: `+ sync_interval_minutes INT DEFAULT 60`,
  `+ next_sync_due_at DATETIME NULL`, `+ discount_alert_pp FLOAT DEFAULT 5.0`;
  backfill: существующим включённым строкам interval 60, `next_sync_due_at =
  NULL` (означает «due сейчас»).
- `competitor_products`: `+ metadata_synced_at DATETIME NULL`,
  `+ is_adult BOOLEAN NULL`, `+ price_miss_count INT DEFAULT 0`,
  `+ last_price_at DATETIME NULL` (backfill из `last_fetched_at`, где цена
  не NULL).
- `competitor_groups`: `+ import_requested BOOLEAN DEFAULT 0`.
- Вторая миграция `migrate_compact_competitor_snapshots.py` — чистка §5.
- `to_dict()` settings маскирует прокси; комментарии про копейки исправляются.

## 12. Тесты (без реальных WB/LLM вызовов)

- `tests/test_competitor_fetch.py` — парсинг synthetic-фикстур search
  v18/catalog/basket, miss-политика, circuit breaker, единый rate limiter.
- `tests/test_competitor_sync.py` — mock fetch: снимки только при изменении,
  miss не затирает current, алерты по порогам, агрегированное Notification с
  дедупом, `next_sync_due_at`, partial при бюджете времени.
- `tests/test_competitor_routes.py` — tenant scope (чужой seller → 404),
  строгая валидация nm_ids, добавление без WB-вызовов, bounds настроек,
  маскировка прокси, bounded search/preview c 429→503.
- `tests/test_competitor_compaction.py` — чанковая компакция, дедуп подряд,
  ретеншн алертов, бюджет времени одноразовой чистки.
- `tests/test_competitor_monitor_loops.py` удаляется вместе с тредами.

## 13. Сопутствующее

- `AGENTS.md`: пункт про `services/competitor_monitor.py` переписывается под
  новую архитектуру в том же изменении.
- Деплой: rebuild из рабочего дерева; миграции fail-fast на старте; старые
  треды исчезают при рестарте контейнера. Ручной VACUUM после чистки снимков —
  опциональная отдельная операция (задокументировать в сообщении к деплою).

## Вне рамок

Автоизменение собственных цен (proposal-контур) — осознанно не входит;
мониторинг Ozon-конкурентов; RRC-контроль; выгрузка отчётов.
