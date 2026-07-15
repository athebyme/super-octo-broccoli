# Фронтенд power-фичи — дизайн-спека

Дата: 2026-07-16 · Ветка: `feature/frontend-power-features` (worktree `.worktrees/frontend-power`, от `a31086a`)

Опирается на уже смёрженный дизайн-слой «Тёплая редакция» (токены, `.sh-*`, `sh-ui.css/js`,
реестр иконок, единые уведомления, ⌘K-палитра, оверлеи с focus-trap).

## Решения продавца

- **Инлайн-редактирование:** только **цена** (остаток отложен — у платформы нет user-пути
  записи остатка в WB). Инлайн-цена **блокирует значение ниже минимальной маржи**.
- **Bulk-публикация в WB:** **строим** новый tenant-scoped эндпоинт.
- **Спарклайны:** пока только токенизация графиков + KPI-спарклайны (строки таблиц — позже).

## Порядок сборки (безопасное → деликатное)

1. Токенизация графиков — фронт-only.
2. ⌘K командный центр (поиск товаров) — малый эндпоинт + палитра.
3. Трей фоновых задач — read-агрегатор + UI.
4. Панель массовых действий — рестайл + «в ИИ-чат» + хендофф цены + bulk-публикация в WB.
5. Инлайн-цена — существующий guardrailed путь + floor маржи.

---

## Ф1. Токенизация графиков (Chart.js + inline-SVG)

**Проблема.** Chart.js 4.4.0, цвета серий захардкожены hex в `<script>` в `analytics.html`
(508–511 спарклайны, 538/547/554/575), `finances.html` (358–363 палитра расходов, 399/408/421),
`finance_detail.html` (inline-SVG donut 55–58/64–75/138). ~13 hex дублируются между 3 файлами.

**Решение (фронт-only).**
- В `base.html :root` + `[data-theme=dark]` добавить категорийную chart-палитру
  `--chart-1..6` (терракота-led, различимые оттенки; тёмные варианты ярче). Значения
  проверяются валидатором dataviz на CVD/контраст перед фиксацией.
- В `static/sh-ui.js` добавить `window.shChart` — helper: `palette()` читает `--chart-1..6`
  через `getComputedStyle(document.documentElement)`; `axis()/grid()/tooltip()` возвращают
  токен-цвета осей/сетки/тултипа для Chart.js; при смене темы (`toggleTheme`) —
  перерисовка/обновление активных графиков (событие `sh-theme-change`).
- Заменить хардкод-hex в трёх файлах на `shChart.palette()[i]` / `shChart.axis()` и т.д.
  KPI-спарклайны — на `var(--chart-*)`. Убрать `Math.random()`-плейсхолдеры спарклайнов
  (`analytics.html:499-502`) в пользу честного «нет данных».
- **Не** трогать `card_quality.html`, `competitors_group_detail.html` (первый — параллельный
  агент; второй — вне скоупа, но при желании подцепится к тому же helper позже).

**Тесты/проверка.** Скриншоты light/dark analytics + finances (Playwright), визуальная
различимость серий; отсутствие хардкод-hex в изменённых `<script>` (grep).

---

## Ф2. ⌘K командный центр — поиск товаров

**Бэкенд (малый).** `GET /api/products/search?q=` в `seller_platform.py` (рядом с `products_list`):
`@login_required`, seller из `current_user.seller`, переиспользовать `or_(vendor_code.ilike,
title.ilike, brand.ilike, nm_id.cast(String).ilike)` из `products_list` (`:1299`),
`load_only(id, nm_id, vendor_code, title, brand)`, `.limit(20)`. Ответ:
`{items:[{id, nm_id, vendor_code, title, brand, url}]}`, `url = url_for('product_detail', product_id=id)`.
Пустой `q` (<2 симв.) → пустой список без запроса.

**Фронт.** Расширить `shCmdPalette()` (`sh-ui.js`): при вводе — debounced (250 мс)
`fetch('/api/products/search?q=')`; результаты товаров рендерятся отдельной группой
«Товары» под статическими разделами; клавиатурная навигация `move()/choose()` учитывает и
динамические узлы; клик/Enter → переход на карточку. Разделы (статические ссылки) фильтруются
как сейчас. Гонки запросов гасятся токеном последнего запроса.

**Тесты.** endpoint: success (tenant-scoped), чужой seller не виден, лимит, пустой q; фронт —
ручная проверка палитры (ввод nmID/артикул/название).

---

## Ф3. Трей фоновых задач

**Бэкенд (read-only агрегатор).** `GET /api/tasks/tray` в `seller_platform.py`:
`@login_required`, seller-scoped, собирает активные операции из уже существующих
seller-scoped таблиц (у всех есть `status`/`progress`/`created_at`/`to_dict()`):
- `BackgroundJob` (status ∈ pending/running) — `models.py:3885`
- `AgentTask` (queued/running, есть `progress_percent`) — `models.py:4209`
- `AutoPublishRun` (running) — `models.py:5295`
- `ImageGenerationExperiment` (status ∈ ACTIVE_STATUSES) — `models.py:1219`
- `EnrichmentJob` (pending/running) — `models.py:2941`
- `PriceChangeBatch` (applying) — `models.py:1799`
- `Seller.api_sync_status == 'syncing'` — синхронизация каталога
Нормализовать в единую форму `{kind, title, status, progress|null, started_at, link}`.
Bounded (по каждому источнику лимит), сортировка по `started_at desc`. Ничего не мутирует.

**Фронт.** Компонент трея рядом с колокольчиком в топбаре (`base.html` + `sh-ui.js` стор
`tasks`): иконка со счётчиком активных, поповер со списком (kind-иконка, заголовок,
`.sh-progress` при наличии %, статус-бейдж, ссылка «открыть»). Поллинг 10–15 с только когда
есть активные (иначе реже); останавливается при 0. Skeleton при первой загрузке, пустое
состояние «нет активных задач». Стиль — как `notification_center.html`.

**Тесты.** endpoint: tenant-scope (чужие задачи не видны), корректная нормализация статусов,
пустой ответ; фронт — ручная проверка.

---

## Ф4. Панель массовых действий (`/products`) + bulk-публикация в WB

**Что уже есть.** На `/products` работает sticky-панель (vanilla JS): чекбоксы строк,
select-all (текущая страница), действия activate/deactivate/export/delete (`/products/bulk-action`),
bulk-edit, bulk-enrich. Контракт «в ИИ-чат» есть в `card_quality.html` (conversations API).

**Фронт (рестайл + доп. действия).**
- Перевести `#bulkActionBar` на `.sh-*` (единый вид с дизайн-системой), добавить `.sh-chip`
  «Выбрано N», кнопки через `.sh-btn`. Оставить существующие действия (reuse их эндпоинтов).
- Добавить кнопку **«В ИИ-чат»** по контракту card_quality: `POST /agents/api/conversations` →
  `POST .../messages` c `{product_ids, entity_kind:'product', scope_mode:'selected'}` (лимит 50),
  редирект в `/agents?conversation=<id>`.
- Добавить **«Изменить цену»** — хендофф выбранных ID в `/prices/change` (передать preselect;
  минимальная правка чтения preselect на стороне prices_change).
- Мобайл: панель как `.sh-bottomsheet` действий.

**Бэкенд — новая bulk-публикация в WB.** `POST /products/bulk-publish`:
`@login_required`, seller-scoped, тело `{product_ids:[int]}` (bounded, напр. ≤200 уникальных
positive int). Семантика (**подтвердить на ревью спеки**): **пере-загрузка локального
контента выбранных `Product`-карточек в WB** через существующий `WildberriesAPIClient.update_cards_batch`
(`wb_api_client.py:1340`) — не создание новых карточек. Инварианты (AGENTS.md):
- составной `Product.id + seller_id`; протектед-поля (цена/остаток) в этом пути **не** трогаются
  (цена — отдельный ценовой путь; остаток — вне скоупа);
- до записи — `CardEditHistory`-snapshot с previous/new (как в per-card reupload и rollback-пути);
- process-wide WB rate-limiter и pacing (как в существующих WB-путях), bounded batch, изоляция
  ошибок per-card (savepoint), честные changed/failed;
- прогресс пишется в **`BackgroundJob`** (`job_type='bulk_publish'`) → операция сразу видна в
  трее задач (Ф3); идемпотентность и повторная проверка ownership/state перед commit.
Кнопка «Опубликовать в WB» в панели дергает этот эндпоинт, показывает тост «Публикация
запущена», прогресс — в трее.

**Тесты.** bulk-publish: success (tenant-scoped, snapshot создан, BackgroundJob обновляется),
чужой seller отклонён, валидация ID (не-int/дубли/пусто), rate-limit соблюдён (мок WB),
частичный сбой изолирован; панель — ручная проверка действий.

---

## Ф5. Инлайн-редактирование цены

**Фронт.** В таблице `/products` (и/или `/prices/change`) ячейка цены становится редактируемой
по клику (Alpine): поле ввода, Enter — сохранить, Esc — отмена, оптимистичный UI + тост.
При вводе — inline-подсказка safety (safe/warning/dangerous) из preview.

**Бэкенд — тонкий безопасный wrapper.** `POST /prices/inline-set`:
`@login_required`, seller-scoped, тело `{product_id:int, new_price:number}`. Внутри —
**тот же** guardrailed код, что и батч-путь, без шортката вокруг него:
1. `calculate_price_changes(change_type='set', product_ids=[id], change_value=new_price)` +
   `SafePriceChangeSettings.classify_change` (±% safe/warning/dangerous, mode notify/confirm/block);
2. **floor маржи:** через `pricing_engine.calculate_price` (`min_profit`) — если новая цена ниже
   минимальной маржи, вернуть `blocked`/`warning` с диагностикой (решение продавца);
3. при mode=block + dangerous → 403; при confirm + dangerous → вернуть `requires_confirm`
   (фронт показывает мини-подтверждение), иначе создать `PriceChangeBatch(1 item)` и `apply`
   через `upload_prices_batch`, обновить `Product.price` + `PriceHistory` (как в батч-apply);
4. optimistic `expected_updated_at` (если передан) — конфликт при поздней правке.
Никакого прямого WB-write в обход `upload_prices_batch`; все guardrails идентичны батч-пути.

**Тесты.** inline-set: safe-цена применяется + PriceHistory; ниже маржи → блок/варнинг;
dangerous под mode=block → 403; чужой seller отклонён; невалидная цена (≤0, не число) отклонена;
optimistic-конфликт → 409.

---

## Общие safety-инварианты (для всех новых эндпоинтов)

- `@login_required`, seller строго из `current_user.seller`; выборка объекта составным `id + seller_id`.
- Никаких прямых WB-write в обход существующих клиентских методов и их rate-limit/pacing.
- Цена/остаток — только через существующие guardrailed пути; не расширять writable allowlist.
- На exception после изменения session — `db.session.rollback()`/savepoint.
- Внешние вызовы — timeout, ограниченный retry, sanitized errors.
- CSRF-токен на мутациях (глобальный fetch-паттерн уже есть).
- Тонкие routes: auth → parse → service → response; доменную логику в services.
- Обновить `AGENTS.md` (новые эндпоинты, трей, bulk-publish, inline-price) в том же изменении.

## Открытый вопрос на ревью

- **Семантика bulk-публикации:** подтвердить, что «опубликовать выбранные в WB» = пере-загрузка
  локального контента существующих `Product`-карточек через `update_cards_batch` (а не создание
  новых карточек из импорта — для этого уже есть `bulk_wb_import` на странице поставщика).
