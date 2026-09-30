# Durable read-only обновление складов и FBS-остатков Ozon — проект

**Статус 26.09: реализовано в candidate worktree; focused и synthetic capacity проверки проходят, final browser/integration и production-приёмка ещё не завершены.** Ниже исходный проект; действующий общий slot, caps/reclaim и подтверждённые измерения описаны в [отчёте выпуска](../operations/2026-09-26-ozon-resumable-reads-and-vue-review.md) и AGENTS.md.

> Исторический текст проекта (статус реализации указан выше).

**Статус: предложение, не реализовано.** Текущие POST в
[`routes/marketplace_commercial.py`](../../routes/marketplace_commercial.py)
синхронно вызывают `sync_warehouses` и `refresh_listing_stocks`. Первый
обходит до 100 страниц складов, второй — до 100 страниц остатков; браузер
ждёт завершения одного HTTP-запроса. Таймаут, закрытая вкладка и перезапуск
не дают продавцу durable прогресс и понятный срок следующей попытки.
Цель пакета — сделать оба **чтения** возобновляемыми, сохранив последний
полный snapshot и не добавляя ни одного provider write или нового endpoint Ozon.

## Области и HTTP-контракт

| Действие | Точная область job | Существующий provider read |
|---|---|---|
| «Обновить склады» | один seller + Ozon account, `kind=warehouses` | `read_warehouses`, cursor |
| «Обновить FBS/rFBS остатки» | тот же account + один owned `MarketplaceListing.id`, `kind=fbs_stock` | `read_stocks_by_warehouse_fbs`, exact `offer_id` |

Сохранить существующие адреса POST. После локальной проверки seller/account,
CSRF, пустого JSON body и exact listing identity они только ставят job и
возвращают `202 {success:true, refresh:<public status>}` с `Location` на
новый seller-scoped GET статуса. GET читает БД, не расшифровывает ключ,
не вызывает Ozon и не продвигает job. Два клика или две вкладки получают
**тот же** активный ID для той же области; другая карточка получает свою
job. Активная job никогда не сбрасывает `next_attempt_at` или Retry-After.
POST с чужим account/listing — `404`, неверный body — `400`; локальный
известный prerequisite «список складов ещё не загружен» для FBS — typed
`409 warehouse_catalog_required` со ссылкой на обновление складов.
Только новый осознанный POST после terminal состояния создаёт следующую
job, с сохранением общего provider cooldown. Никакого GET-trigger.

Public document: `id`, `kind`, `account_id`, `listing_id` только для FBS,
`status`, allowlisted `code`/человеческий `message`, `active`,
`next_attempt_at`, `pages_loaded`, `requested_at`, `completed_at`,
`last_completed_at`, `snapshot_id` только после полного commit.
Не выдавать credential version, raw cursor, staging, provider body, hash
Client-Id, чужие IDs и произвольный текст исключения. `completed` означает
применённый полный snapshot, а `queued/running/waiting_provider` — только
локальный прогресс. Последнее успешное наблюдение и его дата остаются видны
при любой новой ошибке.

## Durable модель и миграция

Новая `MarketplaceWarehouseReadJob`: seller/marketplace/account FK,
`kind`, nullable `listing_id`, сохранённые на enqueue exact
`offer_id`/`product_id` для FBS, приватный
`credential_fingerprint`, `status`,
`next_due_at`, `cooldown_until`, `lease_token`/`lease_expires_at`,
`last_attempt_at`, `failure_count`, `page_count`, `next_cursor`,
`seen_cursor_hashes_json`, `staged_count`/`staged_bytes`,
`error_code`, `warehouse_sync_id` для завершённого account run,
времена создания/обновления/завершения. Два partial unique индекса
для активных `queued|running|waiting_provider` областей:
`(account_id,kind)` у `warehouses` и `(account_id,listing_id,kind)` у
`fbs_stock`; CHECK требует `listing_id IS NULL` только для складов.
`waiting_access`, `completed`, `failed`, `cancelled` терминальны для
автоматической отправки. `cancelled` требует отдельного seller-scoped
CSRF POST по exact job ID: он прекращает лишь локальное продолжение,
а поздний ответ уже начатого provider read не проходит lease-token CAS.
`MarketplaceWarehouseReadItem` хранит лишь
нормализованный ограниченный item, ключ `(job_id,external_warehouse_id)`,
тип области, байты/fingerprint; FK на job с cascade. Повтор склада между
страницами и несовпадающие offer/product блокируют завершение.

`MarketplaceWarehouseSync` остаётся журналом **полного** списка складов,
`MarketplaceWarehouse` и `MarketplaceWarehouseStock` — текущими
проекциями. Для FBS job ID служит snapshot ID; отдельный stock-run нужен
только если UI/история потребуют долговременную последовательность
наблюдений. Не втискивать эти scopes в `MarketplaceReadRequest`: его
`period_code`/date-window CHECK и unique `(account_id,domain,period_code)`
не выражают точный listing, а rebuild действующей таблицы затронул бы
остальные read-домены. Additive fail-fast migration создаёт новые таблицы,
FK/CHECK/индексы, сверяет их на повторном запуске и не backfill-ит
исторические uncertain/failed rows. Подключить после существующих
warehouse/read-request миграций в `docker-entrypoint.sh`, прямой startup
`seller_platform.py`, `migrations/run_all_migrations.py` и профиль
`migrations/run_scoped_batch.py`. При реализации обновить `AGENTS.md`,
runbook и seller guide в том же изменении.

## Worker и атомарность

Повторно использовать **существующий** 10-секундный singleton
`ozon_requested_reads` в `services/product_sync_scheduler.py`, без нового
потока или отдельного scheduler instance. Его wrapper распределяет один
bounded read slot между нынешними `MarketplaceReadRequest` и новыми
warehouse jobs по due/last-attempt, с due-фильтром **до** LIMIT, ротацией
между аккаунтами и максимум одной job одного account за tick. Нужны
synthetic 60+ accounts и один большой account, чтобы доказать отсутствие
голодания. Переиспользовать принцип `ozon_read_scheduler`: короткий
compare-and-set lease с token, process-shared claim и
`try_account_operation_lock`, re-ground account/credential/listing после
lock, отсутствие SQLite write transaction через HTTP. При изменении
private credential marker, отключении account или изменении exact listing
identity job останавливается с безопасным кодом; новая ротация ключа
не продолжает старый cursor под другой identity. Lease старше срока
может быть перехвачен только после истечения и проверки token; старый
worker не вправе записать checkpoint или завершить projection.

Маркер реквизитов — **точно тот же**, что уже применяет durable
`ozon_account_sync._fingerprint(account)`: SHA-256 от exact
`external_account_id + NUL + credentials_encrypted` (см.
[`services/ozon_account_sync.py`](../../services/ozon_account_sync.py)).
При реализации можно вынести чистый helper в общий private модуль, но
алгоритм и текущий catalog-job должны остаться совместимыми. Маркер
хранится только в private job row; API, HTML, логи и audit history его
не возвращают. HTTP enqueue вычисляет его без расшифровки. Worker под
общим account lock повторно читает owner, **тот же Client-Id** и marker
до provider I/O, после restart и перед final apply. Same-Client-Id
замена ключа меняет ciphertext и останавливает старую job; изменение
label/default VAT само по себе её не останавливает. Пере-шифрование
того же ключа также изменит marker и безопасно остановит job. Поле
`credential_version` — версия encryption envelope, не счётчик замен;
`account.version` охватывает и изменения настроек/roles. Ни одно из них
не служит key-only identity или заменой этому marker.

Один проход читает не более **12 physical calls**, начинает вызовы не
позже **45 секунд**, использует `(3,6)` connect/read timeout и
`read_retries=0`, как нынешние bounded Ozon read workers. Transport
обязательно проходит существующий `OzonRateBudget`: локальные 40
calls/s на Client-Id и 20/s на endpoint — внутренние потолки, **не**
обещание upstream-квоты. `429` сохраняет полный Retry-After и в общем
ledger, и в `next_due_at/cooldown_until`; нет sleep и раннего HTTP после
рестарта. Прочие transient ошибки получают bounded 60s exponential
backoff до 6h с deterministic jitter, но никогда раньше provider due.
Возраст job — максимум 24h и до 8 counted failures; перерыв по бюджету
или занятый account не расходует failure quota. Непредставимый Retry-After
останавливает job с `retry_delay_out_of_range`, не сокращается до 24h.
Если реальный provider due позже 24h возраста job, job завершается
`failed/provider_cooldown_exceeds_job_age` с сохранённым публичным
`next_attempt_at`; новый POST до этого срока получает локальный `429`
без Ozon HTTP. После due продавец может явно поставить новую job.

Один нормализованный page и его cursor/строки staging сохраняются **одной
короткой транзакцией** с lease-token CAS. После commit следующая job
начинает с сохранённого cursor; crash до commit повторяет лишь эту read
page и upsert не дублирует строки. Cursor-history хранит bounded SHA-256,
проверяет цикл; raw cursor хранится только в private job row. На прежней
границе 100 pages для каждого вида, request `limit=100`, плюс отдельный
проектный cap **10 000 staged rows / 8 MiB normalized payload / 24h**;
превышение — terminal `response_too_large`, без частичного применения.
Существующий FBS parser допускает до 1000 items в одном ответе; поэтому
проверять суммарный cap, а не считать request limit гарантией ответа.
Отдельный предложенный cap — **2 MiB на HTTP response**, с bounded
streaming download до JSON decode; `Content-Length` и фактические bytes
проверяются независимо. Его надо проверить на synthetic maximum fixtures.

Только после наблюдённого конца pagination worker под тем же account lock
сверяет private credential marker, тот же Client-Id, exact listing
identity и наличие всех warehouse IDs. Затем одной **атомарной**
транзакцией применяет всю staged проекцию:
для складов обновляет/создаёт записи и помечает исчезнувшие недоступными;
для FBS заменяет наблюдения одного listing, включая подтверждённый
**пустой полный** ответ как отсутствие остатков. `completed_at`, публичный
snapshot ID и `status=completed` фиксируются в той же транзакции.
Ошибочная/неполная страница, неизвестный склад, 401/403/429, timeout,
нехватка бюджета и crash не трогают last-good проекцию. **Длительность
финального SQLite write commit для 10 000 строк пока неизвестна.** Это
открытый capacity gate: до принятия реализации измерить финальную
транзакцию и lock contention на восстановленной копии с максимальным
scope и параллельными local reads. Если она не укладывается в
согласованный write-lock budget, сначала спроектировать generation/pointer
публикацию либо уменьшить явно поддерживаемый предел. Частичные видимые
commits недопустимы. Полная pagination не гарантирует один физический
момент времени Ozon; UI показывает время завершённого наблюдения,
не обещает абсолютную консистентность provider snapshot.

## UI и машинные состояния

Vue показывает `idle → queued → running → completed` и отдельные
`waiting_provider` (с точным «после …»), `waiting_access`, `failed`,
`cancelled`. Коды ограничены, например:
`provider_rate_limited`, `provider_unavailable`, `read_budget_exhausted`,
`account_busy`, `access_denied`, `credentials_changed`,
`account_unavailable`, `identity_changed`, `unknown_warehouse`,
`invalid_snapshot`, `response_too_large`, `job_expired`,
`retry_exhausted`, `retry_delay_out_of_range`,
`provider_cooldown_exceeds_job_age`. Новую job не объявлять
успехом по HTTP `202`; список/контекст перечитывать лишь после
`completed` и совпавшего scope/snapshot ID. Pending/error показывают
дату last-good рядом со значениями. `unknown_warehouse` даёт переход
к обновлению списка, `waiting_access` — к настройкам кабинета.

После reload GET восстанавливает активный exact scope; смена account,
listing или вкладки отменяет только старый **браузерный** poll, не job.
Пока вкладка скрыта, polling прекращается; при возврате один GET
восстанавливает due/status. `429` не вызывает повторного POST.
Внезапный non-JSON/403/CSRF/session expiry прекращает polling и показывает
понятный вход/повторное чтение; POST с потерянным ответом сначала GET
ищет dedup job, не отправляется автоматически ещё раз. Кнопка остаётся
доступной для явного повтора только после terminal failure, с новым
job ID. На mobile 320/390 px и desktop 768/1440 px, light/dark,
keyboard/focus, длинные названия и due не перекрывают данные.

## Матрица приёмки и команды будущего пакета

| Уровень | Сценарии, которые должны пройти |
|---|---|
| HTTP/API | Только empty-body POST; CSRF/auth, чужой seller/account/listing = 404/403; enqueue и duplicate = 202 с одним ID и нулём provider calls; GET local-only и scope-bounded |
| Pagination | 2+ pages с checkpoint/restart; дубликат warehouse/cursor cycle, wrong offer/product, malformed/partial page, empty complete, 100-page/10k-row/8-MiB пределы; после ошибки last-good bytes/freshness неизменны |
| Concurrency | Два POST, ручной + worker, lease crash/stale token, два worker, same-Client-Id key rotation, settings-only mutation, re-encryption/disconnect, изменение listing identity между страницами и перед apply; private marker никогда не выходит наружу и старый worker не публикует snapshot |
| Cooldown/fairness | 429 с 120s и 3 days Retry-After, 5xx/network/ledger failure; due pre-LIMIT, 60+ accounts, один 100-page account и другой due; нет sleep, ранних calls и голодания |
| UI | loading/pending/due/completed/failure/access; lost POST → GET, reload, hidden/visible tab, stale account/listing response, session/CSRF, keyboard и responsive light/dark; один POST на действие, ноль provider writes |
| Migration/rollout | Additive/idempotent/constraints/FK, model-created DB, rehearsal на восстановленной копии, старые warehouse/stock rows и operation/proposal snapshots побайтово сохранены; отдельно измерен worst-case final apply/lock contention или принят generation/pointer вариант; readonly smoke после deploy |

Предлагаемые offline команды после реализации: `SKIP_SCHEDULER=1 venv/bin/python -m pytest -q tests/test_ozon_warehouse_refresh_jobs.py tests/test_marketplace_warehouses.py tests/test_marketplace_commercial_routes.py tests/test_ozon_read_scheduler.py tests/test_ozon_rate_limit.py` и отдельный Chromium harness с локальным HTTP bridge без внешней сети. Затем общий offline CI образ и migration rehearsal по существующему release runbook. Эти команды здесь **не выполнялись**; документ не разрешает реальные provider reads/writes, subscriptions или deployment.
