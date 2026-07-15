# Production runbook: WB parity и staged rollout Ozon

## 1. Назначение и границы

Этот runbook относится к базовому Ozon lifecycle P0–P11: кабинеты, справочники,
каталог, общая карточка, локальные drafts, ручная публикация/обновление,
цены/остатки через review boundary, auto-publish, аналитика, fulfillment,
финансы, отзывы/вопросы и operational readiness.

Главные инварианты:

- `Product` остаётся legacy WB write model; Ozon никогда не получает fake `nm_id`.
- `ImportedProduct` — единственная canonical карточка и единственный AI parse cache.
- `MarketplaceListing` — channel/account projection, а не вторая canonical карточка.
- P11 backfill/parity не вызывает WB, Ozon или LLM и обрабатывает не больше 200
  `Product` за один batch.
- Ни один Ozon write не повторяется после ambiguous transport/5xx/malformed
  success. Сначала выполняется read-after-write reconciliation.
- Выключение write-флага запрещает новые submission, но не бросает уже
  submitted/polling/uncertain operation.
- Credentials, idempotency keys, exact submitted payload и raw provider response
  не выводятся dashboard/CLI/logging.

Текущий production topology — один host, один web container и singleton
scheduler. Provider-side account locks используют host-local/shared filesystem.
Перед multi-host или несколькими независимыми web containers нужен отдельный
distributed-lock rollout; просто масштабировать текущий Compose горизонтально
нельзя. P11 projection lease уже хранится в SQLite, но это не меняет границу
остальных account operations.

## 2. Feature flags и безопасные значения

| Переменная | Начальное значение | Назначение |
|---|---:|---|
| `MARKETPLACE_WB_PROJECTION_ENABLED` | `1` | bounded local WB backfill/repair |
| `MARKETPLACE_WB_DUAL_READ_ENABLED` | `1` | durable shadow parity sweeps |
| `MARKETPLACE_WB_COMMON_READ_ENABLED` | `0` | запрос cutover списка `/products`; при неготовности автоматически fallback |
| `MARKETPLACE_OZON_ENABLED` | `0` | Ozon UI/read/sync spine |
| `MARKETPLACE_OZON_PUBLICATION_ENABLED` | `0` | ручной product create/update/rollback |
| `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED` | `0` | reviewed price/stock writes |
| `MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED` | `0` | account-scoped auto-publish |

Нельзя включать все флаги одновременно первым deploy. Рекомендуемый порядок:

1. Projection `1`, dual-read `1`, common-read `0`, все Ozon flags `0`.
2. Дождаться exact WB parity для pilot seller.
3. Включить `MARKETPLACE_OZON_ENABLED=1`, подключить только pilot cabinets и
   выполнить read-only catalog/reference smoke.
4. При зелёном dashboard установить `MARKETPLACE_WB_COMMON_READ_ENABLED=1`.
   Если после deploy появится drift, runtime сам оставит `/products` на legacy.
5. Включить manual publication только для одного проверенного pilot account.
6. После ручного create/update/rollback цикла включить commercial writes.
7. Auto-publish включать последним, отдельно в account settings, с малым daily
   capacity и наблюдением минимум один полный цикл.

Глобальный Ozon read flag показывает UI всем sellers, но provider sync/write
возможен только для явно созданного seller-owned active account с credentials.
Для строгого организационного pilot rollout до отдельного seller allowlist
создавайте Ozon accounts только выбранным sellers.

## 3. Deploy и миграция

До deploy:

1. Остановить writers или перевести приложение в maintenance window.
2. Сделать проверяемую копию `data/seller_platform.db` и сохранить рядом её
   SHA-256/размер/время.
3. Убедиться, что `.env` содержит `ENCRYPTION_KEY`; не копировать секреты в
   командную историю, issue или runbook.
4. Оставить Ozon write flags выключенными.

Docker entrypoint fail-fast запускает:

```bash
python migrations/migrate_add_marketplace_listings.py \
  data/seller_platform.db --backfill-limit 200
python migrations/migrate_add_marketplace_product_links.py data/seller_platform.db
python migrations/migrate_add_marketplace_rollout.py data/seller_platform.db
```

Первая миграция переносит максимум 200 отсутствующих WB rows. Остальной объём
должен обработать runtime job; длительный startup больше не считается нормой.
Новая P11 migration только создаёт `marketplace_projection_runs` и не сканирует
каталог.

После старта:

```bash
SKIP_SCHEDULER=1 python scripts/manage_marketplace_rollout.py status --seller-id <ID>
SKIP_SCHEDULER=1 python scripts/manage_marketplace_rollout.py tick --seller-limit 3 --batch-size 200
```

Те же данные доступны seller-у на `/marketplaces/readiness/`. JSON endpoint —
`GET /marketplaces/readiness/` с `Accept: application/json`.

## 4. WB backfill, parity и common read

### Нормальный поток

Scheduler каждую минуту выбирает до трёх sellers по oldest activity. Для каждого
он делает один backfill batch до 200 rows и, когда backfill завершён, один parity
batch до 200 rows. Cursor и target watermark записываются в БД; crash повторяет
только незакоммиченный batch после истечения короткой lease.

Backfill:

```bash
SKIP_SCHEDULER=1 python scripts/manage_marketplace_rollout.py backfill \
  --seller-id <ID> --batch-size 200
```

Parity:

```bash
SKIP_SCHEDULER=1 python scripts/manage_marketplace_rollout.py parity \
  --seller-id <ID> --batch-size 200
```

`cutover_ready=true` требует одновременно:

- число legacy и WB projections совпадает;
- отсутствующий `Product.id` не найден;
- последний backfill completed и покрывает текущий max `Product.id`;
- последний parity completed после backfill и после последнего изменения
  `Product`, projection или direct `ImportedProduct.product_id` mapping;
- `missing_count=0` и `mismatched_count=0`.

Флаг common read не отменяет эти проверки. Если он `1`, но parity не exact,
`/products` остаётся на `Product` и показывает `legacy_fallback`. Если новая
карточка или отсутствующая projection появляется уже между readiness-check и
выполнением запроса списка, SQL-level gate возвращает полную legacy membership
в рамках самого запроса и не допускает скрытой карточки.

### Repair

Full repair всё равно выполняет только один batch за вызов:

```bash
SKIP_SCHEDULER=1 python scripts/manage_marketplace_rollout.py backfill \
  --seller-id <ID> --batch-size 200 --force-full
```

После completed repair обязательно запустить новый full parity. Mismatch sample
содержит только local product/listing IDs и имена полей, без title/description.
Конфликтующая non-null canonical link не перетирается backfill-ом: она остаётся
видимым mismatch и требует ручного разбора.

Для failed/paused run:

```bash
SKIP_SCHEDULER=1 python scripts/manage_marketplace_rollout.py resume --run-id <RUN_ID>
```

Pause допустим только между batches; активная lease защищает выполняющуюся
транзакцию:

```bash
SKIP_SCHEDULER=1 python scripts/manage_marketplace_rollout.py pause --run-id <RUN_ID>
```

## 5. Ozon preflight перед первым write

Для pilot account проверить:

- connection status `connected`, credential не expired;
- Ozon category/type tree и используемые schemas/dictionaries имеют fresh
  successful snapshots;
- полный catalog sync прошёл `ALL` и `ARCHIVED` до `completed`;
- listing ↔ canonical link exact или вручную подтверждён;
- draft после свежей validation имеет status `valid`, explicit price/VAT,
  physical units, media URLs и exact dictionary IDs;
- `/v1/roles` подтвердил нужную capability;
- operation quota доступна;
- readiness не показывает `uncertain` operations.

Read-only shape probe не использует production web credential storage и не
печатает provider values:

```bash
python scripts/probe_ozon_read_contracts.py --env-file /tmp/ozon_live.env
```

Файл должен принадлежать текущему пользователю и иметь mode `0600`. После smoke
его следует удалить штатным secret-management процессом. Реальные credentials
никогда не добавляются в git.

## 6. Проверка одного write

Первый production-like smoke выполняется только на одной заранее выбранной
карточке:

1. Зафиксировать exact live baseline и screenshot readiness.
2. Не снижать цену. Для price test допускается только повышение, затем отдельный
   reviewed rollback при отсутствии drift.
3. Для stock test допускается только точный owned FBS warehouse: установить `0`,
   дождаться exact read-after-write, затем отдельным reviewed proposal вернуть
   исходное значение.
4. Product create/update идёт только через UI operation journal, не через curl к
   provider endpoint.
5. После submission не нажимать повторно при timeout/5xx. Проверить operation
   `attempt_count`, task status и live listing.
6. Rollback запускать только если current live fingerprint всё ещё равен
   submitted state.

Batch write разрешается только после успешного single-item цикла. Exact-set
response должен содержать каждый item ровно один раз; missing/foreign/duplicate
item означает partial/uncertain, а не success.

## 7. Реакция на типовые provider-сценарии

### HTTP 429 / rate limit

- Read endpoint может повториться bounded с `Retry-After`, максимум 30 секунд.
- Write endpoint автоматически не повторяется.
- Снизить scheduler/manual cadence, оставить operation journal без ручного
  изменения статуса.

### Quota exhausted

- Новая operation остаётся deferred/failed до write boundary.
- Не увеличивать local capacity вручную и не удалять active reservations.
- После следующего provider quota read продолжить только never-attempted rows.

### 401/403 или credential expiry

- Выключить новые Ozon writes, не удалять credential при active/uncertain
  operations.
- Проверить account connection, заменить key через UI под account lock.
- Submitted operations продолжают только read reconciliation после валидной
  credential replacement; слепой replay запрещён.

### Partial async result

- Operation остаётся `partial` либо `uncertain` с per-item sanitized results.
- Не создавать тот же import повторно. Сверить каждый offer live read-ом и
  создать отдельную осознанную operation только для доказанно отсутствующих
  items.

### Provider drift

- Pre-write drift завершает proposal/rollback как conflict до side effect.
- Post-write третье состояние остаётся `uncertain`; не объявлять его success.
- Обновить local snapshot только штатным catalog sync, затем создать новый
  reviewed diff/proposal.

## 8. Аварийное отключение и восстановление

### Provider outage или подозрение на ошибочный write

1. Выключить в указанном порядке:
   `MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=0`,
   `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=0`,
   `MARKETPLACE_OZON_PUBLICATION_ENABLED=0`.
2. Не выключать scheduler и не удалять credentials: уже attempted operations
   должны продолжить reconciliation.
3. Открыть `/marketplaces/operations/`, отфильтровать
   `submitting|submitted|polling|uncertain` и сохранить IDs.
4. Сделать backup DB до ручных действий.
5. Для `uncertain` использовать manual stop только если бизнес принимает, что
   upstream outcome остаётся неизвестным. Эта кнопка освобождает local quota, но
   не превращает outcome в success/failed.

### Повреждение/потеря DB

1. Остановить web, scheduler, agent runtime и image workers.
2. Сохранить повреждённый файл отдельно; не перезаписывать единственную копию.
3. Восстановить последний проверенный backup и SQLite WAL/SHM согласованным
   способом.
4. Запустить все idempotent migrations.
5. До включения writes сравнить operation journal с live Ozon read state.
6. Любая operation с `attempt_count>0`, отсутствующая в backup после write
   window, считается потенциально выполненной upstream. Её нельзя replay-ить;
   создаётся incident/reconciliation record.

### Rollback application deploy

- Сначала common read и все Ozon write flags установить `0`.
- Additive P11/Ozon tables и columns не удалять.
- Старый runtime продолжает использовать legacy WB tables.
- После возврата нового runtime backfill/parity возобновляются с durable cursor.

### Reference corruption/provider shrink

- Не очищать last-good cache вручную.
- Shrink/duplicate/cursor guards сохраняют предыдущий usable snapshot.
- Исправить credential/provider issue и повторить полный reference sweep.

## 9. Наблюдаемость и merge/deploy gate

Перед staged seller:

- [ ] Полный test suite зелёный.
- [ ] Миграции дважды проходят на копии production DB.
- [ ] `git diff --check` и `py_compile` зелёные.
- [ ] WB backfill/parity exact для pilot seller.
- [ ] Common-read flag остаётся fail-safe при искусственном mismatch.
- [ ] Ozon reference/catalog read smoke зелёный.
- [ ] Dashboard не содержит Client-Id, encrypted credential или raw payload.
- [ ] Нет `uncertain` operations без владельца/плана разбора.
- [ ] Backup restore проверен хотя бы на staging-копии.
- [ ] Topology остаётся single-host/shared-lock либо distributed lock внедрён
  отдельным изменением.

Для merge P11 не требует реального provider write. Live write smoke относится к
staged deploy после review branch и выполняется по разделу 6.
