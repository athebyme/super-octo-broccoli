# Ozon и marketplace-neutral архитектура — мастер-план

Статус: P0–P6 implemented; P7 product UX/suppliers/auto-publish next
Дата аудита контрактов: 2026-07-15
Владелец: Seller Hub
Главный принцип: Ozon добавляется через общий контракт маркетплейса, без регрессии WB и без размножения `if marketplace == ...` по routes/services.

## 1. Что именно входит в «поддержку Ozon»

Фича считается законченной не тогда, когда платформа умеет один раз отправить JSON в `/v3/product/import`, а когда один seller может безопасно подключить один или несколько кабинетов Ozon и использовать с ними тот же жизненный цикл, который уже существует для WB:

1. Подключение кабинета, проверка ключа, диагностика scopes/expiry и безопасное хранение credentials.
2. Синхронизация категорий, типов товаров, атрибутов и значений справочников.
3. Импорт уже существующего каталога Ozon в локальную read model.
4. Сопоставление товара поставщика с Ozon-категорией и типом.
5. Детерминированная валидация всех обязательных полей и атрибутов до LLM и до HTTP write.
6. Создание и полное обновление карточки, асинхронное ожидание результата, сохранение ошибок по каждому offer.
7. Работа с изображениями, габаритами, весом, штрихкодами, вариантами и объединением карточек.
8. Цены, минимальные цены и остатки — только через существующий human-review/proposal safety boundary для агентных действий.
9. FBO/FBS/rFBS: кабинеты, склады, остатки, отправления и статусы без смешения fulfillment-моделей.
10. Аналитика, финансы, отмены, возвраты, отзывы, вопросы и карточное качество.
11. Поставщики, авто-публикация, enrichment, фотостудия, инфографика, pricing и unified AI.
12. Tenant isolation, аудит, snapshots, conflict-aware rollback, rate limits, квоты и reconciliation.
13. Marketplace selector во всех релевантных UI/API/agent scopes.
14. Наблюдаемость, runbook, feature flags и безопасный поэтапный rollout.

## 2. Аудит текущей WB-архитектуры

### 2.1 Runtime-поток WB

```text
seller WB token
  -> seller_platform.py / WB-specific routes
  -> WildberriesAPIClient
  -> Product (WB remote projection, identity = nm_id)
  -> ProductStock / WBOrder / WBSale / WBFeedback / WBRealizationRow
  -> analytics, finance, quality, pricing, reviews, warehouse UI

supplier CSV/API
  -> Supplier -> SupplierProduct
  -> global AI parse + WB category/schema mapping
  -> ImportedProduct (seller draft)
  -> WBProductImporter / AutoPublishService
  -> WB async processing + Product binding

unified chat
  -> AgentTask + typed Product/ImportedProduct scope
  -> deterministic skill or bounded LLM
  -> authenticated internal API
  -> local snapshot/proposal/write
  -> WB API where the workflow explicitly owns the external side effect
```

### 2.2 Что уже можно переиспользовать

- `Marketplace`, `MarketplaceCategory`, `MarketplaceCategoryCharacteristic` и админский UI дают полезный каркас для structured reference truth.
- `Supplier`/`SupplierProduct` содержат нейтральный fact pack: название, описание, бренд, фото, размеры, материалы, габариты, страна, цена и остаток.
- `ImportedProduct` является seller-scoped draft и естественной точкой подготовки карточки к публикации.
- `AgentTask`, proposals, snapshots, checkpoints, cancellation и rollback уже задают правильные safety boundaries.
- Pricing engine, prohibited words/brands, content writer, photo/infographic pipeline и quality scorer можно вынести за marketplace adapter.
- Существующие freshness/version/hash guards WB являются образцом fail-closed reference sync.

### 2.3 Что сейчас жёстко привязано к WB

| Контур | Текущая привязка | Почему нельзя просто добавить Ozon ID |
|---|---|---|
| Seller credentials | `Seller.wb_api_key` | Ozon требует `Client-Id + Api-Key`, нужны несколько кабинетов и отдельные статусы |
| Published catalog | `Product`, обязательный `nm_id` | Ozon имеет `product_id`, seller `offer_id` и разные SKU по источникам/FBO/FBS |
| Draft | `ImportedProduct.wb_subject_id`, `wb_nm_id` | Ozon категория — пара `description_category_id + type_id` |
| Supplier mapping | `SupplierProduct.wb_*`, `ai_marketplace_json` | Один JSON нельзя безопасно использовать для двух разных схем |
| References | `subject_id`, integer `charc_type` | Ozon attributes имеют string type, dictionary, complex/group semantics и max count |
| Publications | `WBProductImporter` | Ozon import асинхронный, full replacement и имеет отдельную operation quota |
| Photos | WB media/save и WB CDN indexes | Ozon принимает публичные URL в product import/pictures import и иначе модерирует медиа |
| Prices/stocks | WB endpoints и WB models | Ozon price/stock identity может быть `offer_id` или `product_id`; stock всегда warehouse-scoped |
| Orders/finance | `WBOrder`, `WBSale`, `WBRealizationRow` | Ozon postings и новые finance feeds имеют другую статусную/денежную модель |
| Quality | WB subject cache + WB funnel fields | Нужен marketplace-specific schema context и normalized performance facts |
| AI scope | `entity_kind=product` фактически означает WB `Product` | Нужен typed `marketplace_listing` + marketplace/account scope |
| UI | WB labels, links, IDs и обязательный WB token | Нужен marketplace/account selector и capability-aware actions |

### 2.4 Главный архитектурный долг

Сущность `Marketplace` сегодня названа обобщённо, но её поля и сервисный контракт описывают только WB. `Product` — не master product, а опубликованная WB-карточка. Поэтому безопасная стратегия — strangler migration:

1. Не переименовывать и не ломать `Product` одним большим DDL.
2. Ввести общий seller-scoped account/listing/draft/operation слой.
3. Backfill-ить WB в общий read model и временно держать `Product` как WB projection/compatibility extension.
4. Переводить feature-контуры на общие query/service contracts по одному.
5. Удалять legacy WB-only branches только после измеренного parity и отдельной миграции.

## 3. Актуальные особенности Ozon Seller API

Основной источник — официальная документация Seller API: <https://docs.ozon.ru/api/seller/>. Изменения и даты отключения методов сверяются с официальным каналом обновлений: <https://t.me/s/OzonSellerAPI>.

Документация Ozon меняется без привязки к версии всей API, поэтому endpoint version нельзя хранить как одну строку `api_version`. Версия выбирается отдельно для каждой capability, а compatibility manifest фиксируется в коде и тестах.

### 3.1 Авторизация и кабинет

- Base URL: `https://api-seller.ozon.ru`.
- Заголовки: `Client-Id`, `Api-Key`, `Content-Type: application/json`.
- Один seller может иметь несколько кабинетов Ozon; `Client-Id` является external account identity, API key — секрет.
- Нельзя использовать глобальный admin key для seller reads/writes.
- Global reference sync может использовать отдельный явно настроенный reference account; случайный seller key не выбирается автоматически.
- Ключи имеют роли и могут иметь срок действия; health check сохраняет только capabilities/status/expiry metadata, но не секрет.

### 3.2 Категории, типы и атрибуты

Актуальный flow:

1. `POST /v1/description-category/tree` — дерево description categories и конечных product types.
2. `POST /v1/description-category/attribute` — схема для точной пары `description_category_id + type_id`.
3. `POST /v1/description-category/attribute/values` — category/type/attribute-scoped значения словаря с пагинацией.
4. Search values используется для больших/частично кэшированных словарей.

Старые `/v2/category/tree`, `/v3/category/attribute` и `/v2/category/attribute/values` не используются: Ozon объявил их deprecated ещё в 2023 году.

Критичные поля схемы:

- `id`, `name`, `type`, `is_required`;
- `dictionary_id`, `max_value_count`;
- `attribute_complex_id`, `complex_is_collection`, `is_collection`;
- `category_dependent`, `group_id`, `group_name`, `description`.

Один `type_id` без `description_category_id` недостаточен в доменном контракте, даже если upstream ID сегодня глобально уникален.

### 3.3 Создание и обновление карточек

- `POST /v3/product/import` — создать или полностью обновить до 100 товаров.
- Передаётся полный актуальный payload, включая `description_category_id`, `type_id`, attributes, media, barcode, dimensions, weight, currency, VAT and prices.
- С 10.07.2026 `items.offer_id` явно обязателен, а `items.images360` удалён. Новый контракт не сохраняет и не отправляет `images360`.
- Category requirements меняются независимо от endpoint version: атрибут 22232 (ТН ВЭД ЕАЭС) обязателен для множества категорий, а гарантийный срок с 04.05.2026 стал strict dictionary. Поэтому required/value truth берётся только из fresh type-scoped cache, не из prompt/constants.
- Нельзя трактовать HTTP 200 как опубликованную карточку: метод возвращает task identity, результат опрашивается через `POST /v1/product/import/info`.
- Ошибки сохраняются на уровне каждого offer/attribute, а не превращаются в один общий exception.
- Перед write вызывается `POST /v4/product/info/limit`. С 24.02.2026 Ozon применяет общий лимит товарных операций, а с 09.06.2026 response содержит `operation_limits`.
- Локальная очередь резервирует quota до HTTP. Deferred хвост не объявляется успешно обработанным.
- Повтор ambiguous write не выполняется вслепую: сначала import task/reconciliation по `offer_id` и live product state.
- Реализованный P5a route использует `/v3/product/import` только для нового offer:
  exact preflight по `ALL` и `ARCHIVED` блокирует существующий товар. Несмотря на
  общий create/update upstream метод, локальные create и update workflows не
  смешиваются, потому что у них разные before-state и rollback semantics.
- `operation_limits`, добавленный 09.06.2026, имеет приоритет над legacy counters;
  присутствующая, но malformed новая форма блокирует write и не падает обратно на
  оптимистичный legacy limit.

### 3.4 Идентификаторы Ozon

| Идентификатор | Назначение | Наш контракт |
|---|---|---|
| `Client-Id` | кабинет продавца | `SellerMarketplaceAccount.external_account_id` |
| `offer_id` | стабильный артикул продавца | обязательный unique identity внутри account |
| `product_id` | Ozon product identity | `MarketplaceListing.external_product_id` |
| `sku` | SKU конкретного источника/схемы | typed identifiers JSON, не primary key |
| `description_category_id` | категория описания | category external id |
| `type_id` | тип товара | category type external id |
| `task_id` | async import operation | `MarketplaceOperation.external_task_id` |
| `warehouse_id` | seller/Ozon warehouse | отдельная warehouse identity |
| `posting_number` | отправление | normalized marketplace order/posting identity |

Нельзя склеивать `product_id`, `offer_id` и `sku` в одно поле или приводить все значения через `int()`: offer может быть произвольной строкой, большие IDs хранятся как opaque string на доменной границе.

### 3.5 Цены и остатки

- Prices: read через `/v5/product/info/prices`; update через `/v1/product/import/prices`.
- Stocks: aggregate read через `/v4/product/info/stocks`; FBS warehouse details — актуальная v2-версия; update — `/v2/products/stocks`.
- Seller warehouses: `/v2/warehouse/list`; v1 отключён 07.04.2026.
- Stock write всегда содержит `warehouse_id`; общий `quantity` без fulfillment scope не является корректным Ozon side effect.
- Price/stock writes агента проходят proposal + human review. Adapter не вызывается из LLM tool напрямую.
- Ozon promotions/marketing price не должны перетираться расчётной ценой без явной preview и guardrails.

### 3.6 Fulfillment и заказы

- FBO и FBS/rFBS хранятся как разные source/status families.
- FBS list/get использует актуальные posting methods и сохраняет posting-level status history.
- Методы, запланированные к отключению 01.08.2026, не вводятся в новый код; compatibility manifest выбирает replacement.
- Маркировка, экземпляры, акты и отгрузка — отдельный поздний milestone с обязательным human workflow. Это не часть базовой публикации карточки.
- Push/event capabilities используются как invalidation signal, но polling reconciliation остаётся источником восстановления после потерь.

### 3.7 Аналитика и финансы

- Product analytics нормализуется в marketplace-neutral facts (`views`, `cart_adds`, `orders`, `revenue`, conversion rates), сохраняя raw metric name/version.
- Premium-only metrics обязаны возвращать `unavailable_by_plan`, а не нули.
- `/v3/finance/transaction/list` и `/v3/finance/transaction/totals` отключены 06.07.2026 и запрещены в новом adapter.
- Новый finance ingestion строится на актуальных feeds:
  - `/v1/finance/accrual/by-day`;
  - `/v1/finance/compensation`;
  - `/v1/finance/decompensation`;
  - realization/cash-flow reports там, где нужен бухгалтерский срез.
- До live contract fixtures finance capability остаётся feature-flagged read-only; запрещено молча падать обратно на отключённые v3 endpoints.

### 3.8 Отзывы и вопросы

- Reviews API может зависеть от тарифа/подписки; отсутствие права — capability state, не auth failure всего кабинета.
- С 17.04.2026 нельзя комментировать пустые отзывы без content; UI/agent preflight проверяет content type до write.
- Generated reply является draft/proposal. Отправка ответа — отдельное подтверждаемое действие с audit trail.

## 4. Целевая архитектура

```text
                         +--------------------------+
                         | MarketplaceRegistry      |
                         | code -> typed adapter    |
                         +------------+-------------+
                                      |
                 +--------------------+--------------------+
                 |                                         |
        +--------v---------+                       +--------v---------+
        | WBAdapter        |                       | OzonAdapter      |
        | wraps current WB |                       | Ozon API client  |
        +--------+---------+                       +--------+---------+
                 |                                          |
        +--------v------------------------------------------v---------+
        | marketplace-neutral services                               |
        | accounts | references | listings | drafts | operations     |
        | prices | stocks | orders | analytics | quality | proposals |
        +--------+------------------------------------------+---------+
                 |                                          |
     +-----------v-----------+                 +------------v-----------+
     | legacy WB projections |                 | Ozon read/write models |
     | Product/WB* retained   |                 | no fake nm_id          |
     +-----------------------+                 +------------------------+
```

### 4.1 Adapter contract

Adapter объявляет capabilities, но не принимает ORM objects и не решает tenant authorization.

```python
class MarketplaceAdapter:
    code: str
    capabilities: frozenset[str]

    def check_connection(credentials) -> ConnectionCheck: ...
    def fetch_category_tree(credentials) -> Mapping: ...
    def fetch_attribute_schema(credentials, category_ref) -> Mapping: ...
    def fetch_attribute_values(credentials, attribute_ref, cursor) -> Mapping: ...
    def list_products(credentials, cursor, filters) -> Page: ...
    def get_products(credentials, identities) -> list[Mapping]: ...
    def submit_products(credentials, payloads) -> AsyncSubmission: ...
    def get_submission(credentials, task_id) -> AsyncResult: ...
    def get_operation_limits(credentials) -> OperationLimits: ...
    def read_prices(credentials, cursor) -> Page: ...
    def update_prices(credentials, payloads) -> BatchResult: ...
    def read_stocks(credentials, cursor) -> Page: ...
    def update_stocks(credentials, payloads) -> BatchResult: ...
```

Обязательные свойства:

- stable, validated input DTOs;
- read POST и write POST имеют разные retry policy;
- credentials никогда не логируются и не входят в exception text;
- `Retry-After` и provider request id сохраняются;
- raw response нормализуется только после strict envelope validation;
- capability version задаётся per endpoint;
- side effects имеют idempotency/reconciliation policy.

### 4.2 Новые доменные сущности

#### SellerMarketplaceAccount

Seller-scoped подключение кабинета:

- `seller_id + marketplace_id + external_account_id`;
- encrypted API key, non-secret client/account id;
- label, active/default flags;
- settings (fulfillment defaults, warehouse mapping) без секретов;
- connection status, capabilities, roles, expiry and last check;
- timestamps and sanitized error.

#### MarketplaceListing

Общая published read model:

- seller/account/marketplace scope;
- optional links to `ImportedProduct` and legacy WB `Product`;
- `offer_id`, external product id and typed identifiers;
- category/type references;
- title, status, visibility, moderation and errors;
- content/media/attributes/dimensions/barcodes;
- price and stock summaries plus raw bounded snapshots;
- upstream timestamps and sync fingerprint.

#### MarketplaceProductDraft

Marketplace-specific projection seller draft:

- `ImportedProduct + marketplace + exact operational account` для Ozon;
- exact category/type selection;
- validated attributes with value IDs and display values;
- content/media/price payload overrides;
- schema version/hash and validation result;
- AI inference policy/confidence per proposed field;
- optimistic version and timestamps.

`SupplierProduct.ai_parsed_data_json` остаётся marketplace-neutral fact pack. WB/Ozon payload больше не записывается в один `ai_marketplace_json`.

#### MarketplaceOperation

Durable journal для async/batch work:

- operation kind, seller/account/listing/draft;
- idempotency key and request fingerprint;
- external task ID;
- queued/submitted/polling/succeeded/partial/failed/uncertain/cancelled;
- quota reservation, attempts, next poll and deadline;
- sanitized item results/errors and provider request IDs.

#### MarketplaceListingSnapshot

Before/after snapshot для content/price/stock writes:

- exact source fingerprint;
- expected live state;
- applied and rollback state;
- conflict-aware rollback; no blind full replacement.

#### MarketplaceAttributeValue

Category/type/attribute-scoped dictionary cache:

- external value id and canonical display value;
- availability/freshness/version;
- no global reuse across unrelated Ozon categories;
- paginated sync checkpoint and search support.

### 4.3 Legacy compatibility

- `Product` остаётся WB projection до завершения migration wave.
- `MarketplaceListing.legacy_product_id` связывает backfilled WB listing.
- Existing routes продолжают работать с WB по умолчанию.
- Новый service API требует explicit `marketplace_code`/`account_id`; отсутствие scope разрешается в `wb` только внутри временного compatibility facade и метится telemetry.
- Новые Ozon rows никогда не получают fake `nm_id` и не вставляются в `products`.

## 5. Reference data и админка

### 5.1 Structured truth

Категории/типы/атрибуты/значения Ozon, как и WB schema, являются typed SQL truth. Они не помещаются в RAG или prompt constants.

### 5.2 Freshness policy

- Complete category tree: refresh every 24h и event-triggered invalidation.
- Enabled type schemas: refresh-ahead after 24h; hard TTL 48h.
- Required/small dictionaries: eager cache; large dictionaries: paginated/lazy search + bounded local cache.
- Removed/disabled nodes and values остаются исторически, `is_available=false`.
- Empty, malformed, duplicate or anomalously shrunk snapshot не заменяет last good state.
- Required attribute change немедленно инвалидирует affected drafts; публикация блокируется до revalidation.
- Admin custom AI instructions/restrictions имеют explicit source and are not overwritten by sync.

### 5.3 Admin UI

- Global marketplace list показывает adapter, endpoint versions, reference credential and sync health.
- Ozon card требует Client-Id + API key для reference account.
- Category browser показывает full path, description category, type, disabled/available, schema age and draft impact.
- Attribute detail показывает type, required, complex/group, dictionary coverage, freshness and bounded values sample.
- Manual restriction является platform allowlist поверх official dictionary, но не может добавить значение, которого нет в fresh official scope.

## 6. AI parsing и mapping

### 6.1 Разделение двух задач

1. **Fact extraction**: один раз извлечь из supplier source только наблюдаемые факты и provenance.
2. **Marketplace mapping**: сопоставить эти факты с конкретной fresh schema WB или Ozon.

LLM не должен возвращать готовый HTTP payload и не получает write tools.

### 6.2 Ozon mapping pipeline

```text
supplier fact pack
  -> deterministic category candidates from admin taxonomy/mappings
  -> human or bounded typed category selection
  -> zero-LLM schema preflight
  -> deterministic exact mappings (units, dictionaries, aliases)
  -> LLM only for still-missing textual/extractable attributes
  -> strict exact-set structured output
  -> server-side dictionary/value-id resolution
  -> required/full-card validation
  -> MarketplaceProductDraft proposal
  -> human confirmation / auto-publish policy
  -> async Ozon operation + reconciliation
```

### 6.3 Запрещённые inference patterns

- Не выдумывать вес, габариты, состав, сертификацию, ТН ВЭД, страну, гарантию или бренд «по здравому смыслу».
- Не fuzzy-apply dictionary values. Fuzzy разрешён только как suggestion.
- Не считать `type_id` или category из текста достаточным без fresh admin reference.
- Не генерировать reviews claims, bestseller/top/new labels или неподтверждённые характеристики.
- Не переносить Ozon value ID между category/type scopes.
- Не использовать LLM как classifier перед exact mapping/regex/enum/SQL.

### 6.4 Prompt и budget contract

- Stable schema/tool prefix before dynamic product facts.
- В prompt только атрибуты текущей category/type и bounded dictionary samples.
- Большой dictionary разрешается read-only search tool, максимум bounded calls.
- Batch prefetch exact-set, chunks and Python-owned write path аналогичны unified WB batch safeguards.
- Output содержит каждый `draft_id/product_id` ровно один раз; чужой/повторный/пропущенный ID блокирует chunk.
- Usage/cost сохраняются отдельно для category planning и attribute mapping.

## 7. Functional parity matrix

| Feature | Общий контракт | WB adapter | Ozon adapter | Rollout gate |
|---|---|---|---|---|
| Connections | marketplace accounts | legacy token bridge | Client-Id + key | P1 |
| Reference admin | taxonomy/schema/value | existing | description category/type/attribute | P2 |
| Existing catalog import | listing sync | Product backfill | product list/info | P3 |
| Supplier draft | marketplace draft | WB mapping | Ozon mapping | P4 |
| Manual create publish | async operation | WB importer bridge | v3 import + status | P5a |
| Update/media compensation | versioned operation | media/save | full import/pictures/archive | P5b |
| Prices | proposal/apply adapter | current WB | Ozon price import | P6 |
| Stocks | warehouse-scoped proposal | WB warehouse | Ozon FBS warehouse | P6 |
| Auto-publish | capability pipeline | current | quota-aware Ozon | P7 |
| Product UI | listing query | WB projection | Ozon listing | P7 |
| Quality | normalized content/performance | WB config/funnel | Ozon schema/analytics | P8 |
| Analytics | normalized facts | WB raw tables | Ozon analytics | P8 |
| Orders | normalized order/posting | WBOrder | Ozon FBO/FBS | P9 |
| Returns/cancels | normalized events | WB data | Ozon returns/postings | P9 |
| Finance | normalized ledger | WB realization | Ozon 2026 feeds | P9 |
| Reviews/questions | interaction adapter | current | plan-aware Ozon | P10 |
| Unified AI | typed listing scope | product compatibility | marketplace_listing | P10 |
| Image lab/content | source listing adapter | current | Ozon media | P10 |

## 8. Implementation waves

### P0 — audit and guardrails

- [x] Map WB data/runtime/UI/AI dependencies.
- [x] Verify current Ozon category/product/price/stock endpoint families.
- [x] Record 2026 deprecations and finance cutover.
- [x] Choose strangler migration instead of fake Ozon `Product.nm_id`.
- [x] Add architecture decision tests/lints preventing direct new Ozon calls outside adapter.

Definition of done: master plan accepted by codebase documentation; no production behavior changed.

### P1 — accounts, registry and HTTP spine

- [x] Marketplace adapter interface and explicit WB/Ozon registry.
- [x] Ozon client with strict auth, timeouts, retry classes, request IDs, redirect denial and sanitized errors.
- [x] SellerMarketplaceAccount model/service/routes/UI with strict Fernet encryption and tenant scope.
- [x] Marketplace capability and per-endpoint version manifest.
- [x] Ozon/WB connection tests with mocked HTTP only.
- [x] Idempotent migration, static marketplace seeds and documented WB compatibility policy.

Реализовано в `feature/ozon-marketplace`: Ozon UI по умолчанию dark (`MARKETPLACE_OZON_ENABLED=0`), legacy admin WB actions reject Ozon definition, а отключение feature flag всё равно позволяет удалить seller credential. Live Ozon smoke намеренно не выполняется unit-тестами.

Definition of done: seller can save/test Ozon credentials without secret disclosure or cross-tenant access.

### P2 — Ozon references

- [x] Seed Ozon Marketplace definition.
- [x] Sync/validate category+type tree.
- [x] Sync attribute schemas and scoped dictionary values.
- [x] Freshness/version/hash/checkpoint/shrink guards.
- [x] Admin browse/toggle/instruction/restriction UI.
- [x] Scheduler refresh-ahead and non-blocking cross-process claims.
- [x] Fixture tests for malformed types, duplicates, disabled nodes and partial pagination.

Реализовано в `feature/ozon-marketplace`: global reference credential физически
отделён от seller operational accounts; category/type identity хранится точной
парой; disabled ancestry распространяет availability вниз; schema/dictionary
snapshot применяется только после полного строгого ответа. Последний успешный
hash/timestamp остаётся usable до hard TTL 48 часов даже после неудачного
refresh-attempt. Dictionary checkpoint является только наблюдаемым прогрессом:
retry всегда начинает sweep с cursor 0, потому что безопасного resume без staging
таблицы быть не может. Admin allowlist может лишь сузить fresh official dictionary.

Definition of done: selected Ozon schema is usable as typed SQL truth with no LLM call.

### P3 — listing read model and catalog import

- [x] MarketplaceListing model and WB backfill.
- [x] Ozon paginated product list + batched attributes/prices/stocks sync.
- [x] Exact offer/product/SKU identities and status normalization.
- [x] Full-snapshot missing/archive handling only after complete sweep.
- [x] Unified listing list/detail APIs and marketplace/account filters.

Реализовано в `feature/ozon-marketplace`: WB rows backfill-ятся идемпотентно
через `legacy_product_id`, а Ozon rows никогда не попадают в `Product` и не
получают fake `nm_id`. `MarketplaceCatalogSync` хранит phase/cursor/total и
возобновляется после bounded pause или failure. Каждая list page сначала
полностью обогащается current product info v3, attributes v4, prices v5 и
stocks v4; foreign/conflicting identities, cursor loop, duplicate или total
drift блокируют page commit. Sweep проходит `ALL`, затем `ARCHIVED`; archived
listing остаётся available, а локальные unseen rows становятся unavailable
только finalizer-ом полного run. Seller UI/API всегда начинает с
`current_user.seller` и поддерживает marketplace/account/status filters.

Definition of done: an existing Ozon catalog is visible, tenant-scoped and reconcilable without writes.

### P4 — drafts, category mapping and validation

- [x] MarketplaceProductDraft and schema binding.
- [x] Preserve WB-specific supplier payload as legacy only; Ozon never consumes `ai_marketplace_json`.
- [x] Ozon deterministic state validator and exact unit/value resolver.
- [x] Seller-scoped category mapping UI and correction feedback.
- [x] AI fact extraction vs marketplace mapping split with field provenance.
- [x] Required attributes, complex attributes, dimensions, media, barcode, price and VAT validation.

Реализовано в `feature/ozon-marketplace`: `MarketplaceProductDraft` связывает
seller, Ozon account, `ImportedProduct` и точную category/type pair; edit требует
optimistic `expected_version`. `MarketplaceCategoryMapping` уникален в
seller/marketplace/supplier-or-source/category scope, и автоматически применяется
только active exact mapping. `services/marketplace_fact_pack.py` переносит в facts
только bounded observed/seller-current данные с provenance; legacy AI sections
остаются `unverified_suggestions` и не подставляются в physical/compliance поля.
Старый full-parse prompt переведён на `explicit_only`, удалены весовые эвристики и
default упаковка 20×20×30.

Validator не вызывает Ozon или LLM. Он fail-closed проверяет connected account и
credential presence, source fact drift, required `offer_id`, fresh tree/schema и
словари, exact dictionary ID+display+admin restriction, ordinary/complex attribute
semantics, data types/counts, явные positive dimensions/units, public media URLs,
barcodes и explicit price/old_price/VAT/currency_code=RUB для текущего rollout.
`images360` запрещён согласно изменению 10.07.2026.
Результат содержит machine-readable errors/warnings и schema hash/version; UI
находится на `/marketplaces/drafts/`. P4 сам не выполняет side effect; кнопку
ручного write добавляет только отдельный P5a-контур и feature flag.

Definition of done: draft can be proven publishable/blocked with exact structured reasons.

### P5a — durable manual create publication

- [x] Full whitelist-only `/v3/product/import` payload builder for one validated draft.
- [x] Strict offer/title/attributes/description-4191/media/barcode/physical/price/VAT contract; `images360` absent.
- [x] `/v4/product/info/limit` preflight, typed `operation_limits` parsing and local quota reservation.
- [x] Durable `MarketplaceOperation` + `MarketplaceListingSnapshot` committed before write.
- [x] Create-only live preflight across `ALL` and `ARCHIVED`; existing offer fails before quota/write.
- [x] Task polling, exact per-item result normalization and listing projection finalization.
- [x] Ambiguous transport/malformed-success reconciliation without automatic write retry.
- [x] Separate dark write flag, seller-scoped audit UI/API and minute scheduler recovery.
- [x] Account-level lock across submission/reconciliation, credential edit, connection check and disconnect.
- [x] Tenant/CSRF/strict JSON/no-secret/migration/recovery/deadline tests.

Реализовано в `feature/ozon-marketplace`: новый write требует одновременно
`MARKETPLACE_OZON_ENABLED=1` и `MARKETPLACE_OZON_PUBLICATION_ENABLED=1`.
Operation и exact submitted snapshot фиксируются до HTTP; `submitting` и
`attempt_count=1` фиксируются до вызова adapter. Definitive prewrite outage
остаётся безопасной `queued`, а любой неизвестный результат после начала write —
`uncertain`. Scheduler продолжает submitted/polling/uncertain reconciliation
даже после выключения write flag. После 24 часов без подтверждаемого task status
автоматический polling останавливается, но ручная сверка остаётся доступна.

Disconnect отменяет только никогда не отправленную очередь (`attempt_count=0`).
Credentials и account identity нельзя изменить или удалить, пока они нужны
Ozon write/reconciliation. Public API не возвращает payload, idempotency key или
provider raw response.

Definition of done: вручную подтверждённый новый offer честно достигает
`succeeded|failed|partial` либо остаётся явно видимым `uncertain`, не отправляется
повторно вслепую и полностью аудируется локально.

### P5b — update, media lifecycle and compensation

Read-only staging discovery подготовлен отдельным
`scripts/probe_ozon_read_contracts.py`: он проверяет current pictures v2 и
catalog shapes без scalar data и технически не может вызвать manifest write.
Проверка `/v1/roles` редуцируется к фиксированным boolean capabilities для
archive/unarchive, pictures import, price import и stock update; произвольные
названия ролей/методов наружу не выводятся.
Live probe остаётся обязательным staging gate для конкретного кабинета; runtime
контракты дополнительно закреплены synthetic exact-shape fixtures.

- [x] Separate full-state update operation for an existing offer; create path never mutates it implicitly.
- [x] Media ordering/primary-image/current pictures v2 contract; `primary + images <= 30`, optional color image, no images360.
- [x] Live post-write comparison against exact prior/submitted/third state.
- [x] `/v1/product/archive` conflict-aware compensation for an unchanged newly created listing; beta visibility is not substituted.
- [x] Validated prior-full-state rollback for update as a separate confirmed async operation.
- [x] Manual `uncertain` stop with audit reason and local quota release that does not falsify upstream outcome.

Create snapshot становится `rollback_status=available` только после confirmed
listing identity. Archive preflight повторно читает полный state и прекращает
компенсацию при любом drift. Archive write фиксирует attempt до HTTP, не
ретраится и считается succeeded только когда catalog read видит exact product в
`ARCHIVED`. Per-account role probe остаётся rollout prerequisite.

Definition of done: existing offers and full media lifecycle обновляются отдельным
validated workflow, а каждая обещанная compensation подтверждена официальным
контрактом и synthetic contract fixtures.

### P6 — price, stock and warehouses

Aggregate price/stock reads входят в catalog enrichment, а коммерческий write
никогда не использует aggregate stock. Отдельный warehouse service принимает
только полный paginated `/v2/warehouse/list` snapshot и точные FBS/rFBS строки
`listing + warehouse`; адреса/телефоны не сохраняются. Single-item price/stock
workflow реализован как read-only proposal → human approve → повторный live
preflight → committed operation/snapshot → ровно один write → live
reconciliation. Ambiguous/malformed response не повторяет write. Rollback —
отдельный proposal с exact live drift gate и вторым human approval.

- [x] Current Ozon `/v5/product/info/prices`, aggregate stock v4, warehouse v2, per-warehouse FBS v2 and FBO v1 read contracts.
- [x] Complete warehouse mapping and exact FBS/rFBS listing quantities with unavailable reconciliation.
- [x] Human price/stock proposal/apply UI/API with seller scope, optimistic review and pricing guardrails.
- [x] Durable before/submitted/confirmed snapshots, one-attempt writes and exact single-item response accounting.
- [x] Drift reconciliation and second-review exact rollback for succeeded price/stock updates.
- [x] Independent dark write flag plus scheduler recovery that never abandons attempted operations.
- [x] Multi-item approval for 1..100 proposals of one account/kind: one bulk preflight, one exact-set provider write, one bulk read-after-write and separate per-item operations/snapshots/rollback.

Definition of done: no agent/direct path bypasses proposal and every stock write names an owned warehouse.

### P7 — product UI, suppliers and auto-publish

- Marketplace/account selector in product/catalog/pricing screens.
- Supplier import creates separate drafts for each enabled target marketplace.
- Auto-publish settings become unique per seller+account, not one seller row.
- Ozon quota-aware queue, circuit breaker and deferred tail.
- Status/error UI for moderation and import tasks.

Definition of done: WB and Ozon can run concurrently for one seller without shared locks/counters/IDs.

### P8 — quality and analytics

- Common quality input DTO with adapter schema context.
- Ozon content score against fresh required/filterable attributes.
- Ozon analytics sync into normalized metric facts.
- Marketplace-specific reason codes plus common severity/impact.
- Existing UI collections preserve marketplace/account/entity kind.

Definition of done: comparisons never mix WB and Ozon metrics without an explicit normalized definition.

### P9 — orders, returns, cancellations and finance

- Normalized order/posting, status history and fulfillment source.
- Ozon FBO/FBS/rFBS ingestion with current endpoints.
- Return/cancellation events and deduplication keys.
- Ozon 2026 finance feeds; no disabled v3 fallback.
- Reconciliation totals and source-level traceability.

Definition of done: finance/order UI can filter by marketplace/account and totals tie to raw facts.

### P10 — reviews, unified AI, image lab and content

- Ozon review/question capability detection and draft replies.
- `entity_kind=marketplace_listing` with marketplace/account identity.
- Deterministic intents accept explicit marketplace; ambiguity causes clarification.
- Internal tools are adapter/capability-scoped and least privilege.
- Image Lab uses listing media adapter and Ozon image constraints.
- Content factory selection works over unified listings.

Definition of done: AI cannot cross accounts or invoke an unsupported/bypassed write.

### P11 — hardening and migration parity

- Backfill all WB Product rows to MarketplaceListing.
- Dual-read comparison metrics, then common read path.
- Load tests for 200-product batches and large dictionaries.
- Contract fixtures for rate limit, quota, auth expiry, partial async result and provider drift.
- Disaster recovery/runbook, feature flags and operational dashboards.

Definition of done: WB behavior is parity-tested and Ozon is production-ready for staged sellers.

### P12 — optional advanced Ozon operations

- Promotions and pricing strategies.
- Marking/exemplars and FBS shipping workflows.
- FBO supply drafts/cargoes.
- Push subscriptions/content change events.
- Certificates/brand documents where API support and business need are confirmed.

Эти операции не блокируют базовый Ozon catalog lifecycle и вводятся отдельными safety reviews.

## 9. Migration strategy

1. Все DDL идемпотентны и additive.
2. `db.create_all()` поддерживает новые инсталляции; отдельный script обновляет существующие SQLite DB.
3. Migration создаёт Ozon marketplace definition без credentials.
4. Legacy WB token не копируется в логи/JSON. Runtime compatibility service может создать WB account из raw encrypted column без decrypt/re-encrypt migration.
5. Текущий WB listing compatibility backfill идемпотентен, но всё ещё выполняется startup migration; до large-catalog rollout P11 обязан вынести его в bounded resumable job. Документация не считает текущую startup transaction финальным operational design.
6. Unique indexes создаются после duplicate audit; conflicts сохраняются в migration report и не удаляются автоматически.
7. Новые ORM-required columns подключаются fail-fast в Docker entrypoint.
8. Rollback deploy не удаляет новые tables/columns; старый runtime продолжает игнорировать их.
9. `migrate_add_marketplace_operations.py` additive/idempotent создаёт journal,
   exact snapshots, idempotency constraint и partial unique active-draft index;
   Docker запускает его fail-fast после draft/listing prerequisites.
10. `migrate_add_marketplace_product_updates.py` после P6 commercial migration
    расширяет operation/snapshot CHECK contracts для full update/rollback,
    сохраняя existing operations, snapshots и proposal foreign keys.

## 10. Security и safety invariants

- Seller account/listing/draft/operation выбирается составным `id + seller_id`.
- Body/query `seller_id` никогда не определяет user-facing scope.
- `account_id` проверяется на принадлежность seller и marketplace capability перед каждым API call.
- Credentials расшифровываются только в момент создания adapter client, не сериализуются и не попадают в logs/exceptions.
- Global reference credentials и seller operational credentials разделены.
- Price/stock agent write — proposal only.
- Product/content write требует fresh schema, exact category/type and full payload validation.
- Перед side effect сохраняется durable operation/snapshot; после ambiguous result выполняется reconciliation.
- LLM не получает credentials, raw IDs вне typed scope, prices/stocks write tools или unbounded dictionaries.
- Batch IDs — unique positive typed integers для local IDs; external opaque IDs — strict bounded strings.
- No N+1: references, products, prices and stocks use bounded pages/batches.
- Unsupported/deprecated endpoint fails closed; automatic downgrade запрещён.

## 11. Testing pyramid

### Unit

- strict DTO validation and bool/int/string edge cases;
- Ozon envelope/error normalization;
- no-secret serialization;
- category tree/attribute/value normalization;
- payload builder and dictionary resolution;
- quota allocation and retry classification.

### Service/database

- tenant denial for accounts/listings/drafts/operations;
- idempotent upserts and complete-snapshot availability transitions;
- optimistic draft updates;
- async operation state machine;
- snapshot → committed attempt → task/live reconciliation and conflict cases;
- account credential edit/disconnect races, safe queued cancellation and uncertain preservation;
- compensation/rollback tests добавляются только вместе с подтверждённым P5b endpoint contract.

### Route

- success, anonymous, foreign tenant, invalid payload, unsupported capability;
- CSRF for writes;
- secrets absent from HTML/JSON/log captures.

### Contract fixtures

- Store only synthetic/redacted official-shape fixtures.
- No real Ozon/WB/LLM calls in unit tests.
- Every endpoint version upgrade adds before/after fixture and compatibility test.

### Staging smoke

- dedicated test cabinet, one category, one offer;
- read health → refs → draft validation → import → poll → listing sync;
- explicit human cleanup, never production seller data.

## 12. Rollout

1. `MARKETPLACE_OZON_ENABLED=0` и `MARKETPLACE_OZON_PUBLICATION_ENABLED=0`: schema/code dark launch.
2. Enable account UI for admins/internal seller only.
3. Enable reference sync and catalog read-only.
4. Enable draft validation for allowlisted categories.
5. Enable one-off manual publication for allowlisted sellers.
6. Enable prices/stocks proposals.
7. Enable auto-publish only after quota/reconciliation SLOs hold.
8. Expand analytics/orders/finance independently by capability flag.

Publication rollback switch отдельный: его выключение останавливает новые writes и
не отправляет safe queued rows, но submitted/polling/uncertain операции продолжают
reconciliation. General Ozon flag также не должен бросать уже начатый write.
Account disconnect отменяет только queued rows без попытки и сохраняет credentials
для любого потенциально выполненного upstream side effect.

## 13. Observability and SLOs

Metrics by marketplace/account/endpoint without credentials or raw payloads:

- request count, latency, 2xx/4xx/429/5xx;
- quota available/reserved/consumed;
- category/schema/value freshness;
- listing sync lag and drift count;
- async operation age/status and uncertain count;
- item success/failed/deferred;
- proposal apply/rollback/conflict;
- normalized analytics ingestion lag;
- AI usage and validation reject reasons.

Initial targets:

- zero cross-tenant findings;
- zero secret leakage;
- 100% async writes reach terminal or visible `uncertain` state;
- no automatic retry of ambiguous product import;
- reference hard-TTL violations block writes;
- catalog read sync p95 under 15 minutes for staged account size;
- exact accounting: submitted + deferred + failed = selected.

## 14. Decisions already made

1. Ozon rows do not go into `Product` with fake `nm_id`.
2. Seller can own multiple marketplace accounts.
3. Endpoint versions are per capability, not one marketplace-wide version.
4. Ozon category identity is `(description_category_id, type_id)`.
5. AI extracts facts and proposes mappings; Python owns validation and writes.
6. Official reference data is SQL truth, not RAG.
7. Old Ozon category and finance endpoints are not used as fallbacks.
8. WB compatibility is preserved through a strangler/dual-read migration.
9. Create publication is create-only; existing Ozon offer uses a separate full-state update operation.
10. A write attempt is committed before adapter call and an ambiguous result is never auto-retried.
11. Create rollback uses exact `/v1/product/archive` with full-state drift gate; beta visibility is not treated as archive.
12. Account credentials/identity cannot change while an operation is active or needs reconciliation.

## 15. Questions resolved by implementation probes, not guesses

The following values can change by seller plan/account and are deliberately discovered through typed API responses in staging rather than hardcoded:

- exact per-operation quota from `/v4/product/info/limit`;
- enabled API roles and review capabilities;
- optional/premium analytics periods and dimensions;
- warehouse/fulfillment availability;
- large dictionary pagination behavior;
- finance feed retention and page boundaries;
- content-change push subscription support.
- archive/unarchive role availability for each staged seller account.

Any probe result is cached with timestamp and account scope. A missing capability produces an actionable status in UI; it never silently changes business semantics.
