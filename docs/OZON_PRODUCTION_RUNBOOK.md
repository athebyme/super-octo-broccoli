# Production runbook: WB parity и штатный rollout Ozon

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
- Platform-native mass editor и необязательный импорт XLSX из завершённого
  mass-upload run меняют только seller-owned local drafts и сам run; они не
  создают `MarketplaceOperation` и не обращаются к Ozon. Provider write
  по-прежнему требует отдельного явного повтора.
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
| `MARKETPLACE_OZON_ENABLED` | `1` | Ozon UI/read/sync spine; `0` — emergency rollback |
| `MARKETPLACE_OZON_PUBLICATION_ENABLED` | `1` | ручной product create/update/rollback; `0` — остановить новые submissions |
| `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED` | `0` | reviewed price/stock writes |
| `MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED` | `0` | account-scoped auto-publish |

General Ozon и manual publication production-default `1`; это не включает
автономные или коммерческие записи. Для controlled first deploy допускается
временный явный override этих двух флагов в `0` до миграций/read-smoke.
Рекомендуемый порядок:

1. Projection `1`, dual-read `1`, common-read `0`; auto-publish и commercial `0`.
2. Дождаться exact WB parity для выбранного seller.
3. Проверить persistent `ENCRYPTION_KEY`, подключить выбранный кабинет и выполнить
   read-only catalog/reference smoke.
4. Оставить/вернуть general/manual в production defaults `1` и выполнить один
   explicit card-upload smoke.
5. При зелёном dashboard установить `MARKETPLACE_WB_COMMON_READ_ENABLED=1`.
   Если после deploy появится drift, runtime сам оставит `/products` на legacy.
6. После ручного create/update/rollback цикла включить commercial writes.
7. Auto-publish включать последним, отдельно в account settings, с малым daily
   capacity и наблюдением минимум один полный цикл.

Глобальный Ozon flag показывает UI всем sellers, но provider sync/write
возможен только для явно созданного seller-owned active account с credentials.
Организационное ограничение rollout выполняется списком реально подключённых
seller accounts, а не возвратом глобального pilot-banner.

## 3. Deploy и миграция

До deploy:

1. Остановить writers или перевести приложение в maintenance window.
2. Сделать проверяемую копию `data/seller_platform.db` и сохранить рядом её
   SHA-256/размер/время.
3. Убедиться, что `.env` содержит `ENCRYPTION_KEY`; не копировать секреты в
   командную историю, issue или runbook.
4. Оставить auto-publish и commercial write flags выключенными. Если выбран
   maintenance rollout, держать manual publication явно `0` только до read-smoke.

Docker entrypoint fail-fast запускает:

```bash
python migrations/migrate_add_marketplace_listings.py \
  data/seller_platform.db --backfill-limit 200
python migrations/migrate_add_marketplace_product_links.py data/seller_platform.db
python migrations/migrate_add_marketplace_rollout.py data/seller_platform.db
python migrations/migrate_add_ozon_product_type_visibility.py \
  data/seller_platform.db
```

Base reference migration сама additive-чинит отсутствующий legacy
`marketplace_product_types.is_seller_selectable` до создания индекса; следующая
visibility migration идемпотентно подтверждает значение. Это необходимо, потому
что обе команды намеренно остаются в указанном backward-compatible порядке.

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

Отдельный локальный `maintain_marketplace_source_links` постепенно связывает
существующие Ozon/WB карточки по точному ID поставщика. Он не вызывает API:
durable `BackgroundJob(job_type=marketplace_source_link_reconcile)` сканирует
до 200 Ozon listings одного account за batch, максимум три account scopes за
минуту. Примеры доказанного контракта: `id-7725-1364` (Ozon) и
`id-7725-1366` (WB) → source ID `7725`; для Андрея принимаются только
anchored `A`-форматы и уникальный serial внутри supplier scope. После restart
cursor продолжает `MarketplaceListing.id`; explicit seller unlink исключён.
`ambiguous` означает реальный дубль/conflict и требует ручного выбора, а не
fuzzy fallback. Создание недостающей canonical-копии возможно только из
уникальной seller-scoped тройки supplier+WB+Ozon и не является публикацией.
Сам mass-upload не ждёт фонового обхода: перед подготовкой выбранных карточек он
bounded-пакетом повторяет тот же локальный exact preflight. Связанный Ozon listing
задаёт свой opaque `offer_id`, поэтому `id-7725-1364` обновляется как существующая
карточка, даже если WB vendor code равен `id-7725-1366`. Если существующий
source-кандидат ambiguous или был явно отвязан продавцом, item получает
`existing_ozon_listing_link_unresolved`; create с новым суффиксом запрещён.

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

Для выбранного account проверить:

- connection status `connected`, credential не expired;
- Ozon category/type tree fresh; выбранный official type имеет
  `is_seller_selectable=true`. Его schema/dictionaries могут быть ещё не
  загружены: mass flow сохранит `waiting_reference`, bounded minute worker
  обновит только этот demanded type и продолжит run автоматически;
- полный catalog sync прошёл `ALL` и `ARCHIVED` до `completed`;
- listing ↔ canonical link exact или вручную подтверждён;
- draft после свежей validation имеет `status=ready`,
  `validation_status=valid`, explicit price/VAT
  (VAT может быть заполнен из явной настройки account, валюта — RUB),
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

Массовый seller-facing путь находится на `/marketplaces/ozon/uploads/`:

- один запуск принимает до 200 exact seller-owned карточек одного кабинета;
- одно действие «Синхронизировать Ozon» создаёт новую карточку либо выполняет
  full-state update exact-linked опубликованной; это не patch отдельных полей;
- перед create/update выбранные карточки локально связываются с существующим
  каталогом по exact source ID; фактический Ozon `offer_id` сохраняется в draft,
  а не заменяется WB-кодом с другим seller suffix;
- запуск обязателен только после явного подтверждения: JSON API принимает literal
  `confirm_write: true`, HTML form — `confirm_write=1`. Без него операция не
  создаётся;
- HTTP только missing-only гидратирует legacy observed snapshot по exact
  `supplier_product_id`, выполняет three-way source rebase, валидирует полный
  payload и создаёт durable create/update operations chunk-ами до 50; provider
  submission выполняет singleton scheduler;
- rebase обновляет только прежние source defaults, сохраняет seller edits и
  complex groups. Отсутствующие размеры, цена, НДС и обязательные характеристики
  не угадываются;
- выбранный official product type может безопасно заполнить атрибут `8229`
  («Тип») только через одно exact normalized значение его свежего type-scoped
  словаря. Модель `9048` получает source vendor code только для deterministic
  category mapping, где каждая текущая exact-linked карточка группы имеет fresh
  attributes snapshot не старше 48 часов и её Ozon model exact-normalized равна
  observed vendor code. Один unknown/mismatch отключает recipe; raw evidence
  values не сохраняются;
- упаковочные размеры из supplier snapshot принимаются только из явно
  package/pack/«упаковка»-полей. Generic размеры изделия не превращаются в
  упаковку. Полный WB fallback допустим только по exact same-seller FK при
  `last_sync <= 48h`, `isValid=true` и четырёх положительных фактах; tuple
  переносится целиком и не смешивается с partial supplier dimensions;
- price, НДС, ТН ВЭД и признак маркировки не выводятся из категории, похожих
  карточек или WB по догадке. НДС может прийти только из явного account default
  либо карточки; product-specific compliance и цена должны быть наблюдены или
  введены продавцом;
- для update свежий (не старше 48 часов) exact-account catalog snapshot становится
  preservation baseline: текущие Ozon attributes/media/physical/commercial и
  один представимый barcode не исчезают из replace-style payload из-за пробела
  в canonical source. Текущая галерея и primary сохраняются, новые source URL
  только добавляются после неё до 30 слотов;
- baseline fingerprint фиксируется в operation. Перед quota/write scheduler
  независимо читает exact live info+attributes+prices+pictures; несовпадение
  даёт `update_before_state_drift`, `attempt_count=0` и ноль write-вызовов.
  `ozon_listing_snapshot_stale` требует сначала штатный catalog sync, а
  непредставимые multiple barcodes/empty dictionary display/type/media остаются
  поштучным `needs_input`, а не превращаются в потенциально destructive update;
- если current schema больше не принимает часть представимых legacy
  attributes, mass editor показывает точные `attribute_id/complex_id`, причину
  и отдельную незаполненную галочку очистки. Только после seller confirmation
  exact identities сохраняются в `attribute_removals_json`; create этот список
  отклоняет. Required поле одновременно предлагается заполнить заново.
  Исчезнувшая identity, сменившийся type или live fingerprint останавливают
  update до write. Несколько barcode, отсутствующий type ID и непредставимое
  значение атрибута этим путём не маскируются;
- если фид содержит несколько штрихкодов, полный список остаётся в observed
  source, а Ozon draft автоматически использует первый валидный: current import
  contract принимает один `barcode`, поэтому ручная чистка списка не требуется;
- brand policy проверяет observed/current/seller-edited бренд по единому exact
  normalized denylist. `ozon_brand_forbidden` оставляет только эту карточку в
  `needs_input` и не создаёт provider operation; удалять или подменять бренд
  автоматически запрещено;
- после завершения run основной путь «Открыть массовый редактор» работает
  целиком в Seller Hub. До 200 seller-scoped строк группируются по exact
  исходной категории; один явно выбранный official Ozon type применяется к
  отмеченным карточкам группы и при желании сохраняется как manual mapping.
  Верхняя панель массово заполняет выбранное поле совместимых строк, а внутри
  каждой строки доступны цена, полный package tuple в mm/g, описание, только
  недостающие простые required attributes и явный список несовместимых
  legacy-атрибутов текущей карточки. Boolean requirements выбираются как
  «Да/Нет», а обязательное некорректное значение можно заменить в том же
  сохранении. Если bulk-значение оставить пустым, панель берёт первое уже
  заполненное совместимое значение среди выбранных строк: достаточно один раз
  выбрать точный ТН ВЭД в autocomplete, затем размножить display по группе.
  Dictionary ID между типами не копируется — каждая строка независимо разрешает
  exact display в своём fresh type-scoped справочнике. Type search и dictionary
  autocomplete читают локальный fresh official cache и не вызывают provider;
- «Сохранить и проверить» принимает только exact server-rendered rows,
  optimistic `draft_version` и текущий seller/account/imported-product scope
  под account lock. Ошибка одной строки сохраняется рядом с ней; уже
  проверенные строки не откатываются. Результат `ready_to_retry` всё ещё
  локальный: вызова Ozon и `MarketplaceOperation` на этой кнопке нет;
- необязательная кнопка «Скачать XLSX» оставлена для офлайн-работы и формирует
  тот же bounded набор до 200 строк. Жёлтые поля принимают цену, полный package
  tuple в mm/g, описание и точные значения атрибутов; свежие official
  dictionary values доступны выпадающим списком. Для запрещённого бренда
  действие по умолчанию — `ИСКЛЮЧИТЬ`, что исключает только повтор этого run и
  ничего не удаляет;
- обратный импорт XLSX принимает только исходный файл до 2 MiB (не более 20 MiB
  распакованного содержимого), exact contract/run/seller/account, неизменные
  столбцы, уникальные exact IDs и текущую `draft_version`. Формулы, ZIP anomaly,
  чужой/добавленный draft, stale version, partial package tuple, неоднозначное
  или stale dictionary value блокируются. Обновление идёт под account lock;
  успешная строка получает `ready_to_retry`, но operation ещё не создаётся;
- итог хранит action, status, exact completeness и bounded причину каждой
  карточки; полный validation detail остаётся в draft. `already_current` означает,
  что exact live full-state уже совпал и provider write не выполнялся;
- один seller-confirmed exact bind можно сохранить для исходной
  категории/предмета WB; retry применит mapping ко всем однотипным карточкам.
  Кроме того, bulk preflight один раз локально рассматривает уже связанные
  карточки этого seller: минимум две разные canonical карточки среди текущих
  exact-source observations с одним fresh official Ozon type и без единого
  unknown/conflict создают
  `deterministic` mapping автоматически. Один пример, manual/non-exact link,
  stale schema, отсутствующий тип, конфликт или scan свыше 20 000 listings
  ничего не применяет; поздний конфликт переводит автоматический mapping в
  `stale`, но никогда не меняет manual/rejected/corrected решение. Если schema
  ещё отсутствует, item остаётся активным `waiting_reference`: второй retry не
  нужен;
- on-demand reference worker выбирает не больше 3 exact типов и 6 словарей за
  минуту, failed scope имеет cooldown 10 минут. Admin `is_enabled` — только
  дополнительный refresh-ahead, а не доступность типа продавцу;
- после reference refresh локальная подготовка добавляет только отсутствующие
  observed source-backed attributes и не перезаписывает seller values; через
  6 часов без fresh snapshot показывается `ozon_reference_sync_timeout`;
- retry берёт только явные
  `needs_input|ready_to_retry|failed|cancelled`; `excluded|uncertain` никогда
  автоматически не отправляются повторно.

Schema prerequisite: `migrate_add_marketplace_drafts.py` additive-добавляет
`attribute_removals_json` старой таблице, потому что запускается раньше
`migrate_add_marketplace_draft_attribute_removals.py`; dedicated migration
остаётся идемпотентной. Оба runner-а вызывают их fail-fast до runtime.

Пример JSON-запуска из импортированных товаров:

```json
{
  "account_id": 42,
  "imported_product_ids": [101, 102],
  "confirm_write": true
}
```

Для готовых черновиков используется `POST /marketplaces/ozon/uploads/from-drafts`
с `draft_ids` и тем же literal `confirm_write: true`. Повтор
`POST /marketplaces/ozon/uploads/<job_uid>/retry` также требует
`confirm_write: true`, потому что может создать новый committed write.
Основной UI:
`GET /marketplaces/ozon/uploads/<job_uid>/repair`, local-only submit —
`POST /marketplaces/ozon/uploads/<job_uid>/repair/apply`. Локальные type и
dictionary search endpoints привязаны к тому же seller/run/draft scope.
Дополнительные `GET .../repair.xlsx` и multipart `POST .../repair` доступны
только для завершённого run. Оба local repair POST намеренно не принимают
`confirm_write`. После статуса `ready_to_retry` оператор отдельно нажимает
«Повторить готовые» и ещё раз подтверждает возможный provider write.

Основной repair UI работает на Vue; `/repair/classic` сохраняет прежнюю форму. JSON GET возвращает bounded editor и актуальный CSRF с `private, no-store`. Form-urlencoded apply с Accept JSON возвращает outcome/version каждой строки; новый GET после commit отделён от результата POST. После неизвестного ответа запрещён автоматический повтор: сначала сверка, затем явное разрешение conflicts. При повторном входе новый scoped GET обновляет CSRF без сброса ввода. Никакой repair GET не вызывает Ozon/LLM. Подробнее: `docs/design/ozon-bulk-repair-vue.md` и release receipt.

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

- Pre-write drift завершает proposal/rollback/update как conflict до side
  effect. Для product update это `update_before_state_drift` с
  `attempt_count=0`; карточку нельзя replay-ить до нового catalog sync/review.
- Current attributes read Ozon не возвращает import-only `8229` («Тип»).
  Это не drift только когда omission единственный, submitted value является
  одним exact simple official dictionary value свежего выбранного product type
  и дословно совпадает с official type name. Pre-write это доказанный
  `already_current`; post-write требуется также подтверждённый `imported`
  task ID. Без task ID либо при любом втором отличии operation остаётся
  `uncertain`.
- Post-write третье состояние остаётся `uncertain`; не объявлять его success.
- Обновить local snapshot только штатным catalog sync, затем создать новый
  reviewed diff/proposal.
- Rollback full payload может дополнить prior live только тем же доказанным
  import-only `8229`. Если prior state после этого не проходит текущую official
  required schema (например, в нём ещё нет ставшего обязательным ТН ВЭД),
  rollback помечается `unavailable` и не отправляется.

## 8. Аварийное отключение и восстановление

### Предупреждения о сроке ключа и замена из двух вкладок

`ozon_credential_notices` выполняет только локальную проверку наблюдённого срока: первый tick через 60 секунд после старта scheduler, затем раз в 15 минут. Один tick выбирает до 100 due accounts и создаёт до 25 seller-scoped уведомлений за 5 секунд. Пороги: 14/7/1 сутки и expired; после downtime создаётся только текущая степень. Неизвестный срок не даёт уведомления и не считается бессрочным. Проверка не расшифровывает ключи и не вызывает Ozon, LLM или Telegram.

`marketplace_credential_notices` хранит dedup для текущей credential version и наблюдённой даты, независимо от прочтения/удаления Notification. Обе записи коммитятся вместе под account lock. SQLite writer захватывается с timeout 200 ms; прежний timeout восстанавливается до возврата соединения в pool. Не чистить journal ради повторного предупреждения.

Замена ключа того же Client-Id проходит через `POST /marketplaces/accounts/<id>/reconnect` с обязательным `expected_version` просмотренного кабинета. Stale version возвращает 409 до изменения ключа. UI предлагает явное перечитывание и отдельную повторную отправку; фоновое обновление не заменяет просмотренную версию. Обычный endpoint настроек не принимает новый ключ существующего магазина. Старые ручные клиенты должны передавать версию из актуального account response.

Durable uncertain операции сохраняются и не блокируют восстановление доступа. Занятый physical account lock временно блокирует замену. После замены права/срок сброшены в unknown, штатная фоновая проверка заново наблюдает доступ. Повторная публикация, коммерческая операция или сброс попыток из этого потока запрещены.

### Provider outage или подозрение на ошибочный write

1. Выключить в указанном порядке:
   `MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=0`,
   `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=0`,
   `MARKETPLACE_OZON_PUBLICATION_ENABLED=0`.
2. Не выключать scheduler и не удалять credentials: уже attempted operations
   должны продолжить reconciliation.
3. Открыть `/marketplaces/ozon/uploads/` и `/marketplaces/operations/`,
   отфильтровать `submitting|submitted|polling|uncertain` и сохранить IDs.
4. Сделать backup DB до ручных действий.
5. Для `uncertain` использовать manual stop только если бизнес принимает, что
   upstream outcome остаётся неизвестным. Эта кнопка освобождает local quota, но
   не превращает outcome в success/failed.

### Кандидат: разбор исхода и карантин новых записей (ещё не production)

Эта процедура относится к **коду рабочего дерева до приёмки и выпуска**. На текущем production `29a5f754e159…` действует прежняя остановка проверки из пункта 5 выше: она не запрещает новые price/stock/product операции того же товара. Не объявлять новый экран или карантин действующим на production до отдельного release receipt и read-only smoke.

После выпуска владелец откроет исходную Ozon operation из `/marketplaces/operations/` и экран `/marketplaces/operations/<id>/review` («Разбор результата»). Страница сначала показывает сохранённые факты и исход Ozon, затем отдельное локальное решение. Остановка доступна только при `uncertain` после одной физической попытки и требует причины 10–1000 символов, просмотра области и явного подтверждения. Если точный offer/product ID из сохранённой отправки не согласуется, область будет **весь выбранный кабинет**; оператор обязан прочитать это до подтверждения. Товарная область останавливает новые create/update, archive/rollback, price и stock этого товара на всех складах, включая фото внутри полной карточки. Другие кабинеты и WB не затрагиваются. Новая idempotency key, queued job, batch, смена черновика или ключа запрет не обходят.

Постановка решения оставляет исход Ozon `uncertain`, попытку и snapshots без изменений, прекращает автоматическую сверку исходной операции и освобождает только локальную квоту. Позже оператор может отдельно добавить запись в журнал. Для выяснения исхода допустима ручная **read**-сверка уже attempted операции; кнопка остановки, примечание и снятие сами не делают provider write. Снятие появляется только когда существующий typed workflow доказал исход **той же** операции и пользователь просмотрел свежие версии решения/операции. Чужая успешная price operation, текст «проверено», истекшее время и смена ключа не являются доказательством. При неизвестном исходе карантин остаётся активным; не обнулять attempt_count и не создавать blind repeat. После потерянного POST сначала перечитать `/review`; повторное сохранение не делать наугад.

Модель и миграция кандидата: [модели](../models.py) (`MarketplaceWriteQuarantine`, `MarketplaceWriteQuarantineEvent`), [миграция](../migrations/migrate_add_marketplace_write_quarantine.py), [сервис решений](../services/ozon_write_quarantine.py) и [определение области](../services/ozon_quarantine_scope.py). Миграция добавляется **после** `migrate_add_marketplace_media_publications.py` во все три пути старта, без backfill исторических решений. Достаточная локальная проверка схемы и доказательств без сети: `venv/bin/python -m pytest -q tests/test_marketplace_write_quarantine_migration.py tests/test_ozon_write_quarantine.py tests/test_ozon_quarantine_outcome_contracts.py`. Это проверка кандидата, не команда деплоя; внешние backups владелец отложил, текущий порядок production backup выше самовольно не менять.

Перед приёмкой читать число `active` карантинов и записей журнала через SQLite `mode=ro`/`PRAGMA query_only=ON`; ожидаются нули, потому что production smoke не принимает реальных решений. Если владелец сам поставил карантин, остановить автоматическую приёмку и изучить его решение — не удалять и не переписывать строки. Предыдущий runtime image `29a5f754e159…` не применяет новый запрет. Для rollback сначала остановить новые operator decisions и provider mutations (web, scheduler и другие writers), затем под этой паузой повторно прочитать persisted `active` holds и только после этого переключать runtime. Read-only count `0` до паузы не годится: новый hold мог появиться после чтения. Более простой безопасный rollback на старый image — заранее установить `MARKETPLACE_OZON_PUBLICATION_ENABLED=0`, `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=0` и `MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=0` независимо от предварительного count; при любом active hold оставить все три write-флага выключенными до возвращения runtime с guard или отдельного принятого решения по запрету. При откате не удалять hold/event tables, SQLite WAL или общий Ozon rate ledger.

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

Перед расширением на следующего seller:

- [ ] Полный test suite зелёный.
- [ ] Миграции дважды проходят на копии production DB.
- [ ] `git diff --check` и `py_compile` зелёные.
- [ ] На representative create и update detail показывает точные required /
  supplied attributes, фото, штрихкод, content/physical/commercial readiness;
  карточка с отсутствующим фактом остаётся `needs_input`, а не «готовой».
- [ ] Повтор неизменённой опубликованной карточки заканчивается
  `already_current`, `attempt_count=0` и не вызывает `/v3/product/import`.
- [ ] Для типа с обязательным `8229` тот же no-change gate работает при
  единственном provider omission; изменённый `8229`, второй пропуск и любой
  visible drift остаются fail-closed.
- [ ] Изменённая опубликованная карточка создаёт `product_update`, проходит exact
  live preflight и отправляет полный payload ровно один раз.
- [ ] Task-confirmed update с единственным omission `8229` сохраняет actual
  live fingerprint, не повторяет write и даёт rollback только когда exact prior
  payload проходит текущую required schema.
- [ ] Повтор уже завершённого create/update rollback с тем же idempotency key
  возвращает ту же child-operation при неизменном `attempt_count`, даже когда
  parent уже имеет `rollback_status=succeeded` либо listing уже archived.
- [ ] До create/update все source-фото заменены на подписанные immutable JPEG,
  внешний GET возвращает image/jpeg, а operation всё ещё имеет
  `attempt_count=0`.
- [ ] JSON/form/retry без явного `confirm_write` отвергаются до создания job или
  operation.
- [ ] Каждый бренд из `services/ozon_brand_policy.py` блокируется до operation;
  punctuation/case-варианты блокируются, substring вроде `HOT WHEELS` не даёт
  ложного срабатывания.
- [ ] WB backfill/parity exact для выбранного seller.
- [ ] Common-read flag остаётся fail-safe при искусственном mismatch.
- [ ] Ozon reference/catalog read smoke зелёный.
- [ ] UI не содержит API key, encrypted credential или raw provider payload;
  non-secret Client-Id отображается только как идентификатор кабинета.
- [ ] Нет `uncertain` operations без владельца/плана разбора.
- [ ] Backup restore проверен хотя бы на staging-копии.
- [ ] Topology остаётся single-host/shared-lock либо distributed lock внедрён
  отдельным изменением.

Для merge P11 не требует реального provider write. Live write smoke относится к
staged deploy после review branch и выполняется по разделу 6.

## 10. Фото карточек перед create/update

Источник поставщика может открываться в браузере, но возвращать внешнему
crawler HTML challenge вместо изображения. Поэтому `queued` product
create/update до первого Ozon API-вызова автоматически:

1. скачивает bounded-порцию исходных фото через SSRF-safe transport с
   cookie/meta-refresh;
2. проверяет реальные image bytes и сохраняет immutable JPEG по SHA-256 в
   `MARKETPLACE_IMAGE_ASSET_DIR`;
3. атомарно обновляет submitted snapshot/fingerprint при
   `attempt_count=0`;
4. отправляет карточку только после `media_asset_state=ready`.

В mass-upload это видно как «Проверяем фото». `media_preparation_pending` и
`media_preparation_retry` не означают Ozon write; permanent `media_*` failure
оставляет одну карточку с понятной причиной и не блокирует остальные.
Публичный endpoint принимает только HMAC-подписанный digest существующего
файла, не является proxy и не возвращает placeholder.

Production defaults:

- `MARKETPLACE_IMAGE_ASSET_DIR=/app/data/marketplace_image_assets`;
- `OZON_MEDIA_ASSET_URLS_PER_ATTEMPT=3` (1..10);
- `OZON_MEDIA_ASSET_ATTEMPT_SECONDS=40` (10..55);
- `PUBLIC_BASE_URL` — внешний HTTPS origin Seller Hub.

После deploy проверьте один подготовленный URL извне: status 200,
`Content-Type: image/jpeg`, `X-Content-Type-Options: nosniff`, cache policy
`public, max-age=31536000, immutable`. Неверная подпись обязана вернуть 403,
несуществующий либо повреждённый digest — 404.


## Локальные копии и host observer (26.09.2026)

Внешнее хранилище отложено владельцем. Локальные ежедневные copies и observer работают отдельно от Flask/scheduler; потерю всего хоста этот контур не покрывает. Старые архивы не включены в автоматическую ротацию.

```bash
systemctl list-timers seller-local-\* --no-pager
systemctl show seller-local-observer.service seller-local-backup.service -p Result -p ExecMainStatus
venv/bin/python scripts/local_operations.py probe
sudo systemctl start --no-block seller-local-backup.service
```

Backup timer: 03:15–03:20 Europe/Moscow ежедневно, Persistent catch-up. Его запуск не означает успешную копию: проверяйте private `~/.local/share/seller-hub/local-operations/backup-run.json`, где только `status=complete` и `round_trip_verified=true` подтверждают завершение. Manifest остаётся в `/app/data/backups/managed-daily`. Первая копия проходит фактическую распаковку в отдельный файл, проверку SHA/размера и полный `quick_check`; никакого cutover/replay/API write. Две последние managed copies сохраняются после накопления, старые archives в родительском каталоге остаются.

Observer timer: примерно каждую минуту; `observation.json` содержит последнюю read-only проверку, `observer.json` — streak/confirmed incident и результат единственной попытки уведомления. Три последовательных bad samples вызывают агрегированное сообщение всем активным подписчикам deployment-бота, два healthy samples — recovery. Docker starting grace 15 минут не является healthy recovery. `notification.status=reserved|unconfirmed` не повторяется вручную без проверки доставки. `--no-notify` меняет state без отправки: используйте отдельный `--state-dir` для drills, иначе можно подавить настоящее событие.

Для копирования нужно `размер SQLite по page_count + 4 GiB` свободного места (gzip cap 2 GiB + reserve 2 GiB). Перед запуском выполняется maintenance только восстанавливаемого JPEG cache: max 1 GiB, low-water 512 MiB. Web environment не меняется. Cache refill и рост БД могут исчерпать запас; observer это показывает. Во время реально захваченного `.backup.lock` учитывается рабочее место snapshot, но reserve ниже 2 GiB и длительность >35 минут остаются ошибками.

При failed backup изучите безопасные `backup-run.json`, `observation.json` и status service; не заменяйте live DB/WAL и не удаляйте старые архивы. Container SIGALRM 1860s ограничивает exec даже после потери host Docker CLI, однако kill может оставить непринятый `.sqlite-backup-*`. Наличие папки не доказывает копию или отсутствие живой работы: сначала проверить flock/exec, ownership и доступный restore, затем отдельно решать судьбу конкретного временного каталога. Не использовать blanket prune. Повреждённый ownership journal не сбрасывать и чужие архивы в него не добавлять.

Установка/обновление units: `sudo bash scripts/install-local-operations.sh`; она не restart-ит приложение. Сами helper sources находятся в host checkout, права записи в него дают operational control; не менять их во время backup. Services ограничены user/UMask/ProtectSystem/ProtectHome/ReadWritePaths/MemoryMax/TasksMax, но требуют доступа к Docker. Отказ самого observer виден в systemd journal; независимый внешний наблюдатель пока отсутствует.


## Настройки магазина и журнал (принято 26.09, 18:38 МСК)

План: `docs/design/ozon-account-settings-history.md`. `POST /marketplaces/accounts/<id>` теперь меняет только label/default VAT и требует exact Client-Id + `expected_version`. Для key replacement используется отдельный `/reconnect`. Default selection требует просмотренную пару `expected_default_id/version`, disconnect — просмотренную `expected_version`. 409 означает перечитать и проверить состояние, а не повторить тот же POST. Фоновые статусы не дают согласия изменить скрытую просмотренную версию формы.

`GET /marketplaces/accounts/<id>/history?before_id=<cursor>` читает до 30 событий exact seller/account/marketplace с keyset cursor. История начинается с этого выпуска; её отсутствие не доказывает, что старых изменений не было. Key event не содержит ключа и не подтверждает доступ к API. `credential_version` — формат envelope; account version — ревизия состояния. Наружу не выдаются actor ID, crypto, fingerprint, raw provider body или внутренние версии из журнала.

Миграция `migrate_add_marketplace_account_events.py` additive/fail-fast и включена во все startup paths. Mutation и audit в одной транзакции; ошибка audit откатывает account/default fan-out. При неработающей записи не вставлять события вручную и не отключать gate. Caller-owned dirty/flushed session должна завершиться до account service. Group lock + sorted account locks исключают одновременную смену основного кабинета и physical provider operation. Durable uncertain не блокирует безвредные настройки; запрет удаления ключа при attempted operations сохранён.

Rollback приложения сохраняет новую таблицу: прежний образ её игнорирует. Не удалять журнал и не откатывать live DB ради runtime rollback. Production-проверка должна читать реальные формы/историю и сверять fingerprints; ключ, label и НДС владельца ради smoke не меняются. Внешнее backup-хранилище отложено, проверка миграции использует локальный подтверждённый архив.

Приёмка этого пакета: `docs/operations/2026-09-26-ozon-account-settings-history.md`. Runtime `29a5f754e159…`; rollback `e81fd556ead03…`. Локальный verified backup/rehearsal и реальный read-only browser приняты; source keys/label/VAT ради проверки не менялись.
