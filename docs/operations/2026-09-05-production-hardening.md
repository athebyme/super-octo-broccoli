# Seller Hub: проверка рабочего контура, 5 сентября 2026

Цель: привести интеграции WB/Ozon, фоновые процессы, производительность и UX к проверенному рабочему состоянию. Изменения выполнялись поверх существующего незакоммиченного worktree; пользовательские правки сохранены. Внешние публикации, изменения цен и остатков не выполнялись.

## Текущий статус

Работа по выявленным программным причинам завершена. Финальный образ `sha256:bd7e411922cd219b53c2d5a410103e39bc9472c0e71a2d5fc18d927c0ce16f9e` **развёрнут и healthy**: startup 11:48:01 UTC, Gunicorn 11:53:58, первый успешный healthcheck 11:54:02 (~361s). Journal соответствует реальному code/schema (read-only проверка 0,040s). На последнем образе повторены все **39 браузерных навигаций**: HTTP 200, без JS/HTTP errors, broken images и горизонтального overflow. Keyboard/selection/analytics проверки пройдены во всех четырёх сочетаниях theme/viewport; финальные screenshots визуально просмотрены.

Финальный полный pytest: **2266 passed, 469 subtests passed**, 276 warnings, **489,57s**, `/tmp/seller-hub-complete-20260905.xml`. Последняя правка плюс WB credentials/stocks/transport также проверены отдельно: **48 passed, 5,28s**. Последний live `/products` profile: **0,192s / 15 SQL** против исходных 1,714s / 314 SQL. Внешние ограничения доступа/данных перечислены в конце; их нельзя считать исправленными одним deploy.

## Результат и проверенные причины

### Запуск и сохранность данных

- Первоначальный запуск контейнера занимал почти 12 минут: Docker startup, standalone и comprehensive пути повторяли миграции, включая дорогие глобальные FK scans.
- `scripts/startup_migrations.py` сохраняет journal только успешного **полного** bundle и schema fingerprint. Shared lock исключает параллельный startup. Code/schema drift, прерванный запуск либо отсутствие journal требуют полного migration пути. Обычный DML не инвалидирует запись; вручную успешный journal не ставился.
- Digest консервативно включает runtime Python dependencies: изменение даже runtime-only .py вызывает полный bundle. Поэтому смена образа всё ещё требует примерно шести минут; повтор неизменного проверенного bundle занимает доли секунды. Упрощать digest без доказанного dependency closure нельзя.
- Entrypoint fail-fast, без `|| echo`; `--base-only` устраняет повтор post-schema migration. Admin env sync остаётся на каждом старте. Пароли и DB URL из startup-вывода убраны.
- FK safety переиспользует baseline только при неизменных connection/local changes/transaction/schema/data revisions. Полные проверки managed и newly introduced violations сохранены.
- Пройдены чистая установка и два полных rehearsal на отдельной копии 11,8-ГБ БД. Повтор guard на финальной rehearsal DB: **0,389s**, после второго live rollout — **0,225s**.
- Сверка backup/rehearsal: **31 358 Product, 21 076 CardEditHistory, 21 828 SupplierProduct, 20 655 ImportedProduct, 40 077 listing, 15 drafts** сохранены; supplier settings fingerprint совпал.
- У seller 1 найдено 175 legacy Product с nm_id=0 и 145 связанных CardEditHistory. Старый sync пытался удалить Product и падал на NOT NULL FK. Теперь bounded seller-scoped batch до 500 только деактивирует неподтверждённые карточки. Live деактивированы **173** ещё активные строки; все 175 Product и 145 историй сохранены, orphan histories=0.

### WB transport, credentials и остатки

- Центральный reference key действительно истёк: исходные повторные 401 подтверждены. После проверенного backup ключ обновлён через encrypted `Marketplace.api_key`; подтверждены одинаковый WB account, read-only scope и успешный Content ping. Seller credentials и Ozon account не переключались.
- Новый bounded JWT parser — только negative gate: expiry отклоняется до limiter/HTTP; будущий exp не считается доказательством подписи, seller identity или всех прав. `/api-settings` не вставляет сохранённый ключ в HTML и показывает срок без ложных обещаний доступности API.
- Retired Statistics stocks endpoint возвращал 404. [WB объявил замену](https://dev.wildberries.ru/en/release-notes?id=373) на Analytics `POST /api/analytics/v1/stocks-report/wb-warehouses`. Каталог, `/products/sync-stocks` и `/api/warehouse/refresh` теперь только ставят дедуплицированный durable read-only job.
- Stock worker: до 100 exact positive nmID, одна страница до 5000 строк за минутный tick, buffer до 25 000 строк, partial TTL 15 минут. Только observed конец полного batch разрешает атомарно заменить восстанавливаемый ProductStock; malformed/duplicate/scope drift/401/403/429 сохраняют прежние факты. Склады агрегируются по official ID, не имени/hash; отсутствующий quantityFull — NULL/«—», не ноль.
- Single-attempt read POST, redirects запрещены, timeout 15s, process-shared limiter 1/20s fail-fast; 429 сохраняет retry due 60..3600s без sleep. DB write transaction через I/O не удерживается. Public job serializers скрывают временные rows/key fingerprint; старый generic GET timeout не завершает durable stock job.
- Реальные WB 429 выявили скрытый urllib3 GET retry: один учтённый запрос превращался в несколько физических, длительный sleep и RetryError. Brand endpoint теперь имеет отдельный single-attempt adapter без redirects. Общий GET adapter исключает 429 и implicit Retry-After retry; bounded 5xx read retries сохранены. Четыре теста с настоящим локальным HTTP-сервером проверяют ровно один физический запрос, typed WBRateLimitException и отсутствие sleep при max_retries 0/3, с Retry-After и без него. Endpoint pacing и общие Content budgets не сняты.
- **Live full-stock read пока недоступен:** предоставленный WB токен Base (acc=1), а новый метод требует Personal/Service с Analytics. Известный несовместимый тип отклоняется локально; остатки не фабрикуются. Реальное upstream rate limiting также не обходится.
- На финальном образе один actual GET `/content/v2/directory/kinds` вернул 429 за **0,571s**, typed `wb_rate_limited`, без RetryError/sleep. Наблюдённые WB headers: Limit=1, Remaining=0, Reset/Retry=993s. Это фактический upstream cooldown, не выдуманная доступность и не повод повторять диагностические запросы.

### Ozon reads, каталог и безопасные ограничения

- Финальный полный read probe: **14 методов success**, включая FBS/FBO; только review/question — ожидаемые 403/code 7. Write endpoint count=0.
- Исправлены контракты stock probe: FBS — `sku + limit + cursor`, FBO — **`skus` plural** + limit + cursor. Оба подтверждены реальными успешными ответами. Произвольный offset не подставляется.
- Ozon review limit 20..100, questions 1..100. Отказ подписки стал typed `MarketplaceInboxAccessDenied`/403 и scheduler state unavailable/INFO; 24h cooldown и ручная read-only перепроверка сохранены, весь кабинет не отключается.
- Catalog sync ошибочно возобновлял исторический failed run даже после нового completed sweep. Теперь resume допустим только для последнего run в точном seller/marketplace/account scope; старые результаты остаются аудитом.
- Live attributes page выявила 4191/values=[] у 3 из первых 1000 карточек: это наблюдённое пустое описание, теперь оно принимается как отсутствие текста. Multiple/dictionary/complex malformed shapes по-прежнему fail-closed.
- Candidate проверен внутри live filesystem-lock namespace: **run #8 completed, 8719 seen/updated, 0 created, 9 pages, 45 physical read calls, 39,64s**. После deploy обычный service, без candidate и без force restart: **run #9 completed, 8719 updated, 0 created, 9 pages, 45 physical reads, 42,21s**. Это доказало восстановление обычного запуска и обновило локальную read model, не товары на Ozon.
- Harness запрещал все requests с retry_class != read; retries=0, timeout=15s, budget 100 calls/240s.
- On-demand dictionary refresh сохранил старые факты при двух пустых snapshots и shrink guard **77→20**, **84→39**. Freshness не сфабрикована; upstream snapshot требует проверки.
- На отдельной копии 15 настоящих drafts выполнен локальный rebase/reference/account defaults + validation: 14 обработаны без сети, draft #8 защищён существующей uncertain publication. TNVED/marking сохранены. Потерявшие подтверждение source-default dimensions удаляются честно, ручные данные не перезаписываются. Без свежих required dictionaries и подтверждённых текущим source packaging facts готовых карточек нет.
- Historical operation #6: `update_postwrite_drift`, status uncertain, attempt=1, next_poll=NULL. Повторного marketplace write либо принудительного success не было.

### Производительность и UX

- WB `/products`: исходный профиль **1,714s / 314 SQL**, из них 1,606s в enrichment availability (50 per-card resolver calls).
- Добавлен negative-only batch existence preflight по точным seller-owned/connected source keys. Если потенциальных кандидатов нет, N+1 resolver не запускается; positive/ambiguous случаи проходят прежние identity gates без изменения приоритетов и конфликтов.
- На той же копии данных: **0,202s / 15 SQL**. На реально работающем втором финальном образе: **0,194s / 15 SQL**. Browser WB catalog TTFB 174–434ms (верхняя граница во время полного Ozon sweep), против исходных 1567–1848ms.
- На самом последнем образе повторный profile: **0,192s / 15 SQL**. Browser TTFB `/products` 179–368ms (cold/loaded desktop в начале smoke, warm mobile в конце), inventory 6–14ms; регрессии после transport fix нет.
- Draft source picker ищет весь seller-owned canonical каталог вместо первых 200 строк: компактный bounded GET до 20 результатов, Unicode casefold без глобальной подмены SQLite lower. Exact ID не смешивается с identity matching.
- Debounce 250ms, AbortController и response revision gates, keyboard/ARIA, loading/empty/retry, очистка selected ID при изменении запроса. Форма не загружает 100 полных source ORM blobs; выбранный/единственный кабинет подставляется.
- Shared `.sh-btn` получил нормальные default padding/центровку. Inventory: безопасные initial arrays/expressions, устранён double-init, cleanup poll timer, мобильная сетка/min-width, theme token вместо белого фона в dark. Polling каждые 15s только для активного job на видимой вкладке; pending/failure/unknown/freshness различаются.
- Ozon analytics: Chart instance в closure вне Alpine reactive proxy, один init, destroy/revision guards. Быстрое переключение периода не оставляет старый ответ и не вызывает getContext error.
- Actual deployed UI: **39 навигаций** — 18 desktop/light страниц и 7 ключевых страниц в каждом из desktop/dark, mobile/light, mobile/dark (1440/390px). Все HTTP 200, JS/HTTP errors=0, broken images=0, horizontal overflow=0.
- Проверены keyboard selection, закрытие source list, очистка exact ID, отсутствие сохранённого ключа в HTML и быстрые 7d/30d/7d переключения analytics во всех четырёх сочетаниях. Warm inventory 6–12ms TTFB, draft list 22–67ms.
- Снимки визуально просмотрены: `.playwright-mcp/ui-final-optimized-20260905/`; предыдущие `ui-hardening-final-20260905`, `ui-audit-20260905`. Все browser artifacts игнорируются Git.

## Проверки и deployment

- Baseline: **2183 passed, 466 subtests**, 478s.
- Main hardening: **2249 passed, 466 subtests**, 501s.
- Первый release: **2253 passed, 466 subtests**, 519,05s.
- Второй release: **2262 passed, 469 subtests**, 496,13s.
- Финальный release: **2266 passed, 469 subtests**, 489,57s, exit 0.
- Последний transport regression набор: **48 passed**, включая четыре loopback physical-retry проверки.
- Full suite запускается как `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q --tb=short --junitxml=/tmp/seller-hub-complete-20260905.xml`. Legacy SQLAlchemy Query.get и test-return warnings двух старых parsing tests не устранялись глобальным unrelated rewrite.
- `git diff --check`, Python compilation новых/изменённых runtime modules и `node --check static/draft-source-picker.js` пройдены.
- Первый rollout `6b064259...`: 11:01:10 UTC → healthy 11:07:49, ~399s. Второй `b727cfbd...`: 11:28:40 UTC → healthy 11:34:47, ~367s.
- Финальный `bd7e4119...`: 11:48:01 UTC → healthy 11:54:02, ~361s. Последняя RO сверка: 31 358 Product, 21 076 histories, 175 legacy Product / 145 связанных histories, active legacy=0, orphan histories=0. Ozon run #9 completed (8719); operation #6 по-прежнему uncertain/attempt 1. Созданных сегодня MarketplaceOperation с attempt_count>0 — **0**. Оба backup существуют с прежними размерами и mode 0600; deployed retry adapter не повторяет 429 и сохраняет bounded 503 retries.
- Команды: `docker compose build seller-platform`, затем `docker compose up -d --no-deps --no-build --force-recreate seller-platform`. Старый writer останавливается до migration bundle. Live миграции параллельно Gunicorn/scheduler не запускать.
- Rollback image tags сохранены: `seller-hub:pre-hardening-20260905` (fc6b9a0...), `seller-hub:hardening-stage1-20260905` (6b064259...), `seller-hub:hardening-stage2-20260905` (b727cfbd...). Caddy не менялся.

## Backups, credentials и очистка

- Named volume: `super-octo-broccoli_seller_platform_data`; live DB `/app/data/seller_platform.db`.
- Сохранены два согласованных owner-only (0600) SQLite backup, оба **quick_check=ok**:
  - `/app/data/backups/integration-hardening-20260905-consistent.sqlite`, 11 782 828 032 байт, pinned read snapshot, запись 171s.
  - `/app/data/backups/integration-hardening-20260905-predeploy.sqlite`, 11 782 963 200 байт, snapshot около 10:54 UTC, запись+проверка 414,3s. Это предпочтительная predeploy точка возврата.
- Исходный незавершённый backup `integration-hardening-20260905.sqlite` (4 244 635 648 байт) удалён только после проверки полного. Он не является точкой восстановления.
- После завершённых rehearsal удалены только созданные нами четыре staging containers (`seller-hub-hardening-ui`, `seller-hub-startup-final-check`, `seller-hub-startup-populated-check`, `seller-hub-startup-empty-check`), internal network `seller-hub-hardening-local` и копия `/app/data/startup-check-20260905` (~12GB). Live DB и оба backup сохранены; пользователь уведомлён.
- Небольшой host artifact чистой установки: `/tmp/seller-hub-startup-empty.Y0a4b5`. Shared Docker images/volumes глобально не pruning-ились.
- Ключи владельца находятся вне Git/image: `/home/athebyme/.local/share/seller-hub/credentials/`, directory 0700, `ozon-ro.env` и `wb-ro.env` 0600. Значения не выводить; synthetic browser sessions также не печатать.
- Supplier seed больше не содержит credentialed feed URL: новый supplier выключен без URL, raw SQL явно задаёт non-null defaults. Existing admin settings сохранены; full-ingest mapping заменяет только NULL/exact known old seed, custom/current mapping и updated_at не трогает.
- **Credentialed supplier URL ранее попадал в Git history.** История не переписывалась; upstream credential должен быть отозван/ротирован владельцем отдельно. Удаление строки из текущего seed не отзывает опубликованный credential.
- Host HTTP(S)_PROXY мешает исходящим диагностическим запросам; контейнерная сеть работоспособна. Проверки выполнены из Docker, Chromium с --no-proxy-server. Self-signed loopback UI probe может давать benign SSL certificate-unknown warnings в Gunicorn; фактических HTTP/JS ошибок не было.

## Не подменять ограничения доступа и данных

1. Для live WB FBW stocks нужен Personal/Service token с Analytics; нынешний Base token непригоден. Не обходить negative gate и не превращать недоступность в нулевые остатки.
2. Ozon review/question недоступны по подписке/account access. Включение требует внешнего доступа, не изменения обработчика на ложный success.
3. Required Ozon dictionaries с empty/shrunk snapshots, упаковочные факты и compliance нуждаются в подтверждённых данных. Publish gate, unknown write outcome и ручные исправления не обходить.
4. Исторический supplier credential требует ротации у поставщика.

Незавершённых build/deploy/test этапов нет. В контрольном окне после последнего deploy в Gunicorn/application logs не обнаружены Traceback, RetryError, database-is-locked или worker timeout. Проверка не является обещанием отсутствия любых будущих provider ошибок; внешние 403/429, stale dictionaries и unknown write outcomes остаются честными состояниями.
