# Руководство для AI-агентов

## Статус документа

Это корневой источник инструкций для автоматизированной разработки в репозитории. Он относится ко всему проекту, если более вложенный `AGENTS.md` не задаёт узкое исключение.

**Обязательное правило:** при изменении архитектуры, границ модулей, runtime-потоков, команд запуска, переменных окружения, миграций, агентной политики, safety-инвариантов, бюджетов LLM/API, prompt caching или UI-темы обновляйте этот файл в том же изменении. Не оставляйте команды и схемы работы только в коде или сообщении к PR.

## Назначение проекта

Seller Hub автоматизирует работу продавца на маркетплейсах. Wildberries остаётся полностью поддерживаемым legacy-потоком; Ozon вводится поэтапно через marketplace-neutral account/adapter слой без fake `Product.nm_id` и без переключения существующих WB routes на незавершённый общий read model.

Основной интерфейс является Flask/Jinja-приложением. Актуальная AI-архитектура представляет одного seller-facing помощника в формате чата. Один runtime `orchestrator` строит план и вызывает внутренние типизированные skills в одном процессе. Старые отдельные agent workers сохранены только как legacy profile.

## Карта репозитория

- `seller_platform.py`: основной Flask application object, конфигурация, CLI, scheduler bootstrap и регистрация route-модулей.
- `app.py`: отдельный legacy-калькулятор прибыли, локальный порт `5000`; в текущем Compose его нет.
- `models.py`: единый набор SQLAlchemy-моделей, включая sellers, products, agent tasks, chat, proposals и change snapshots.
- `routes/`: UI и HTTP API. Новую бизнес-логику держите в `services/`, а не раздувайте handlers.
- `services/`: доменная логика, интеграции WB, поставщики, pricing, карточки, content factory и фоновые процессы.
- `services/marketplace_adapters/`: типизированный registry и provider adapters. Adapter не принимает ORM objects и не выполняет tenant authorization.
- `services/ozon_api_client.py`: строгий Ozon Seller API transport с endpoint allowlist и разными retry-классами для read POST и write POST.
- `services/marketplace_accounts.py`, `routes/marketplace_accounts.py`: seller-scoped кабинеты маркетплейсов, encrypted credentials и read-only connection checks.
- `services/marketplace_reference_accounts.py`: отдельный admin-owned credential lifecycle только для глобальных Ozon-справочников.
- `services/ozon_reference_service.py`: strict category/type/attribute/value snapshots, freshness, shrink guards, admin restrictions и bounded refresh-ahead.
- `services/marketplace_listings.py`, `routes/marketplace_listings.py`: seller-scoped unified listing read model, resumable Ozon catalog sweep и marketplace/account-filtered UI/API.
- `services/marketplace_product_links.py`: audited exact matching между общей seller-owned `ImportedProduct` и WB/Ozon listing-проекциями; title/LLM fuzzy matching запрещён.
- `services/marketplace_fact_pack.py`: marketplace-neutral observed facts с field-level provenance; legacy AI output остаётся отдельным unverified suggestion. Linked WB projection сравнивается с canonical только по bounded common content (`title/description/brand`): drift виден в Ozon readiness/validation, но никогда автоматически не перезаписывает master.
- `services/marketplace_drafts.py`, `routes/marketplace_drafts.py`: seller/account-scoped Ozon drafts, exact category mappings, optimistic edits и deterministic publishability validation без provider/LLM calls.
- `services/ozon_product_import.py`: whitelist-only контракт `/v3/product/import`, строгая нормализация task status и quota response.
- `services/ozon_product_state.py`: exact full-state reconstruction из info/attributes/prices/pictures, canonical fingerprints и archive contract; raw provider body не сохраняется.
- `services/marketplace_publications.py`, `routes/marketplace_operations.py`: durable seller-scoped Ozon create/full-update/rollback operations, snapshots, manual submit, polling/reconciliation и audit UI/API.
- `services/marketplace_auto_publish.py`: deterministic multi-account draft provisioning и account-scoped Ozon auto-publish queue с quota allocation, atomic cancellation boundary, circuit breaker и durable operation reconciliation; этот поток не меняет WB-shaped `ImportedProduct.import_status`.
- `services/marketplace_warehouses.py`: полные warehouse snapshots и точные FBS/rFBS observations по `listing + warehouse`; адреса и контакты не сохраняются.
- `services/marketplace_commercial.py`, `routes/marketplace_commercial.py`: reviewed price/stock proposals, live drift preflight, single-attempt writes, reconciliation и conflict-aware rollback proposals.
- `services/ozon_commercial_contracts.py`: ORM-free whitelist/exact-set контракты current price, warehouse и stock endpoint families.
- `services/ozon_analytics_contracts.py`: ORM-free exact request/response contract для read-only `/v1/analytics/data`; каждая метрика имеет provider name, unit, definition version и явный запрет неявного сравнения с WB.
- `services/marketplace_analytics.py`, `routes/marketplace_insights.py`: durable account-scoped Ozon analytics snapshots, normalized metric facts, last-good reads и UI/API `/marketplaces/analytics`.
- `services/marketplace_quality.py`: детерминированная Ozon quality projection по fresh type schema, fresh listing snapshot и свежему analytics snapshot; WB `Quality Score v2` не переиспользуется как будто определения совпадают.
- `services/ozon_fulfillment_contracts.py`: ORM-free whitelist-контракты current Ozon postings/returns/cancellation feeds; buyer PII и свободные provider payloads отбрасываются до ORM.
- `services/marketplace_fulfillment.py`, `routes/marketplace_fulfillment.py`: durable account-scoped read-only sync и UI/API `/marketplaces/orders`, `/marketplaces/returns`, `/marketplaces/cancellations`; WB order/finance models не переиспользуются.
- `services/ozon_finance_contracts.py`: ORM-free signed-money/cursor/exact-set contracts для current Ozon accrual by-day/types/postings; top-level fact и nested explanatory fee разделены.
- `services/marketplace_finance.py`, `routes/marketplace_finance.py`: immutable last-good account snapshots и UI/API `/marketplaces/finance`; partial run скрыт, currency/marketplace rollup запрещён.
- `services/ozon_feedback_contracts.py`: ORM-free whitelist-контракты current Ozon reviews v2 и questions v1; status/date/cursor/identity проверяются до ORM, provider links и author fields отбрасываются.
- `services/marketplace_inbox.py`, `routes/marketplace_inbox.py`: durable exact-account read-only inbox `/marketplaces/reviews` и локальные AI/template reply drafts. Provider send в этом контуре отсутствует.
- `services/marketplace_operation_locks.py`: account-level non-blocking file lock для publication, credential mutation, health check и disconnect в одном host/filesystem namespace.
- `scripts/probe_ozon_read_contracts.py`: optional live read-only contract probe для catalog/warehouses, finance accrual types/current-day и capability-proven reviews/questions. Он принимает credentials только из process env или owner-only `/tmp/ozon_live.env`, вызывает исключительно manifest endpoints с `retry_class=read` и выводит только bounded response shapes без scalar values/offer/SKU/customer text/raw errors.
- `templates/`: Jinja2 UI. Общая оболочка и design tokens находятся в `templates/base.html`.
- `static/`: CSS/JS без отдельного frontend build. TailwindCSS и Alpine.js подключены через CDN.
- `migrations/`: идемпотентные SQLite migration scripts. Это не Alembic.
- `scripts/`: init, backup, diagnostics и operational utilities.
- `tests/`: смешанный набор pytest-style и `unittest.TestCase` тестов.
- `docs/`: дополнительная документация. При расхождении команд доверяйте текущим Compose/entrypoint и этому файлу.

### Marketplace-neutral foundation и Ozon

- Полный scope, parity matrix и волны P0–P12 зафиксированы в `docs/OZON_MARKETPLACE_IMPLEMENTATION_PLAN.md`.
- `SellerMarketplaceAccount` является operational account текущего seller: Ozon хранит non-secret `Client-Id` отдельно от Fernet-encrypted API key. Один seller может иметь до 10 кабинетов одного маркетплейса; default выбирается только внутри `seller + marketplace`.
- Новый credential path fail-closed требует валидный `ENCRYPTION_KEY`; fallback на plaintext, сохранённый для legacy WB колонок, здесь запрещён. Секрет не входит в `repr`, JSON/HTML, status/error или logs.
- `MarketplaceRegistry` явно регистрирует `LegacyWildberriesAdapter` и `OzonAdapter`. Endpoint versions хранятся per capability; Ozon transport не принимает произвольный URL/path.
- Ozon read-only POST может bounded-retry transport/429/5xx. Ozon write POST автоматически не повторяется после transport/5xx/malformed success: durable operation переходит в `uncertain` и сначала сверяется по task/offer live state.
- Актуальный Ozon manifest использует description-category v1, product list/info v3, product attributes v4, pictures read v2/write v1, product import v3 + status v1, archive/unarchive v1, limits v4, prices read v5/update v1, aggregate stocks read v4, per-warehouse FBS read v2, per-warehouse FBO read v1, stocks update v2, warehouses v2, analytics data v1, postings FBS v4/FBO v3, returns v1/rFBS v2, conditional cancellation v2, finance accrual by-day/types/postings v1, reviews list v2 и questions list v1. Deprecated category/product endpoints, postings FBS v3/FBO v2, conditional cancellation v1, per-warehouse FBS v1, warehouse v1 и устаревающие finance transaction v3 запрещены; старый review list v1 также не является fallback. По уведомлению Ozon от 14.07.2026 finance v3 отключат 08.09.2026; не добавляйте временный fallback. В `/v3/product/import` `offer_id` обязателен, а `images360` удалён 10.07.2026.
- `services/ozon_commercial_contracts.py` является ORM-free fail-closed boundary для P6: price/stock builders принимают только whitelist-поля и exact identities, response обязан быть exact-set без чужих/повторных/пропущенных результатов, stock всегда содержит точный `warehouse_id`, а warehouse/FBS pages требуют корректную cursor pagination. Platform batch cap равен 100 даже там, где upstream допускает больше. Сам contract layer не даёт права на provider write; разрешённый side-effect path находится только в `MarketplaceCommercialService` после durable proposal, отдельного human approval и повторного live preflight.
- `scripts/probe_ozon_read_contracts.py` не имеет write mode, загружает live credentials только из process env или owner-only файла и выводит только bounded shapes. Из `/v1/roles` он сохраняет только фиксированные boolean-проверки известных методов, не role names и не произвольные method values. Не расширяйте probe endpoint-ом, который не помечен `retry_class=read`.
- Seller account/catalog/draft/commercial/quality/analytics/fulfillment/finance UI и live Ozon checks включаются через `MARKETPLACE_OZON_ENABLED=1`; orders/returns/cancellations/finance sync всегда read-only и не требует write flag. Новый manual product write требует также `MARKETPLACE_OZON_PUBLICATION_ENABLED=1`, Ozon auto-publish дополнительно требует отдельный `MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=1`, а approve price/stock proposal — независимо `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=1`. Все flags default `0` и явно передаются в web container через Compose. Выключение write flag запрещает только новый side effect и отправку queued rows; scheduler продолжает submitted/polling/uncertain reconciliation.
- Ozon reference truth хранится отдельно от WB-shaped `MarketplaceCategory`: `MarketplaceTaxonomyCategory`, `MarketplaceProductType`, `MarketplaceAttributeDefinition` и `MarketplaceAttributeValue`. Идентичность типа всегда `description_category_id + type_id`; value ID никогда не переносится между attribute/type scopes.
- Global Ozon taxonomy использует только явно настроенный `MarketplaceReferenceAccount`, никогда случайный seller key. Tree обновляется каждые 24 часа, stale enabled schemas bounded-пакетом каждые 6 часов; required dictionaries синхронизируются eager в общем dictionary budget. Все scope jobs используют non-blocking file claims.
- Последний полный Ozon snapshot остаётся structured truth до hard TTL 48 часов. Empty/malformed/duplicate/partial/anomalously shrunk ответ не меняет reference rows. Dictionary checkpoint наблюдаемый и не является resume cursor: без staging retry обязан начать с нуля. Admin restriction может быть только exact subset fresh official dictionary; required attribute нельзя отключить.
- `MarketplaceListing` является общей published read projection, но не master product: Ozon row всегда содержит seller/account/marketplace scope и раздельные opaque `offer_id`, `external_product_id` и SKU; WB row является временным backfill с `legacy_product_id` и nullable account. Ozon никогда не получает fake `Product.nm_id`.
- `MarketplaceCatalogSync` хранит durable phase/cursor/total/counters. Ozon sweep идёт по `ALL`, затем `ARCHIVED`; страница list + product info + attributes + prices + stocks полностью валидируется до одного commit. Pause/failure сохраняет cursor и последний read model, но не снимает availability. Missing rows помечаются только finalizer-ом после полного прохода обеих фаз; force restart начинает новый run с первой страницы.
- Seller catalog UI/API находится на `/marketplaces/listings/`, фильтруется только внутри `current_user.seller`, а внешний read запускается только для составного `seller_id + account_id` после connected/active/expiry checks. Один account сериализуется DB running-index и non-blocking file claim; один HTTP запуск ограничен 50 list pages, UI использует bounded 5-page пакет.
- `MarketplaceProductDraft` является отдельной seller/account-specific проекцией `ImportedProduct`, а не HTTP body и не `Product`. `MarketplaceCategoryMapping` уникален внутри seller + marketplace + exact source identity; автоматически применяется только `active` mapping на доступный enabled type. Exact `Product.subject_id` связанной WB projection имеет приоритет; `ImportedProduct.wb_subject_id` допускается только после подтверждённой WB projection (`Product`/`product_id`/`wb_nm_id`/`import_status=imported`). Тогда identity `wb_subject:<id>` сильнее строки категории поставщика и сохраняется seller-wide с `supplier_id=NULL`; неподтверждённый AI/pre-publication `wb_subject_id` не расширяет mapping. Если WB mapping отсутствует, legacy supplier/source category остаётся fallback. Title/category fuzzy match и перенос WB dictionary IDs запрещены. Seller-facing кнопка «Подготовить Ozon» передаёт `validate=true`: она создаёт/переиспользует только локальный draft и сразу запускает deterministic validation, но никогда не вызывает provider write. `mapping_readiness` отдельно показывает актуальность canonical fact snapshot, exact category mapping, schema hash/version, обязательные атрибуты и official dictionaries; stored `ready` не перекрывает ставший stale reference/source/account state.
- Unified chat принимает Ozon selection только как `entity_kind=marketplace_listing` с exact integer `ids`, `marketplace_code`, `account_id` и `scope_mode=selected`. Browser scope считается недоверенным: harness повторно ground-ит полный набор через `seller + marketplace + account`, task хранит listing IDs отдельно от `product_ids`, а internal brief требует exact match с assigned task. Listing IDs никогда не передаются в WB/ImportedProduct skills. `marketplace-listing-audit` читает только локальный snapshot без LLM, `marketplace-listing-insight` делает не более одного bounded model call; оба имеют пустой tool allowlist и не расшифровывают credentials. Marketplace write из этого scope не попадает в legacy content writer и требует отдельного proposal contract.
- Draft UI/API `/marketplaces/drafts/` использует optimistic `expected_version`; draft/account/imported source проверяются составно с seller. Выключенный feature flag блокирует draft writes, но оставляет существующее состояние read-only. WB/Ozon никогда не образуют механический round-trip: Ozon category/type, attribute ID и dictionary value ID остаются в Ozon projection. Будущий Ozon → WB перенос допускается только как reviewed diff общих фактов в canonical `ImportedProduct` с последующей отдельной channel validation.
- Ручной create остаётся create-only: existing offer в `ALL|ARCHIVED` блокирует его до write. Связанный published draft редактируется как новая optimistic revision и отправляется отдельным `product_update`: сервис восстанавливает полный current state из четырёх exact reads, фиксирует prior payload, использует update quota и считает success только после полного live fingerprint match.
- `MarketplaceOperation` хранит durable lifecycle и bounded sanitized results, а `MarketplaceListingSnapshot` — exact submitted/confirmed state. Raw provider response, idempotency key, credentials и submitted payload не возвращаются public routes. Один account write/reconcile сериализуется file claim; scheduler обрабатывает bounded due batch каждую минуту.
- P6 разделяет `MarketplaceCommercialProposal` (human review) и `MarketplaceOperation` (provider side effect). Proposal creation делает только live read; approve повторяет live read и при drift завершает без write. Snapshot и `attempt_count=1` commit-ятся до единственного provider write, после чего разрешены только read-after-write reconciliation; malformed/ambiguous результат не ретраится. Rollback также является новым proposal и создаётся лишь когда live state точно равен original submitted state. `MarketplaceWarehouse` не хранит адреса/телефоны, `MarketplaceWarehouseStock` хранит только точный seller/account/listing/warehouse FBS observation; aggregate stock никогда не является write baseline. Миграция `migrate_add_marketplace_commercial.py` сохраняет старые P5a rows при расширении operation/snapshot CHECK contracts и fail-fast подключена после operation migration.
- Product rollback всегда является отдельным подтверждённым write. Для create он вызывает `/v1/product/archive` ровно для созданного `product_id`, только если full live state не дрейфовал; для update восстанавливает точный prior full payload тем же async import contract. Оба пути commit-ят отдельную operation до write, не ретраят ambiguous response и обновляют parent snapshot. Beta visibility не является archive и не используется.
- `uncertain` можно вручную остановить только через audited `stop_reconciliation_release_local_quota`: outcome остаётся `uncertain`, write не повторяется, credentials продолжают блокироваться. Нельзя превращать эту кнопку в ручное неподтверждённое `succeeded`.
- Изменение Client-Id/API key, connection recheck и disconnect используют тот же account lock. Credential mutation блокируется при любой active operation; disconnect отменяет только `queued` с `attempt_count=0`, но сохраняет ключ для submitted/polling/uncertain и любого write с ненулевой попыткой.
- Legacy WB auto-publish является scope `marketplace_code=wb, account_id=NULL`. Ozon settings/run/item всегда привязаны к одному exact seller-owned account и имеют независимые lock, daily counter, retry history и circuit breaker. Supplier import создаёт локальный draft для каждого enabled Ozon target без LLM/provider вызова; pause блокирует provider writes, но не deterministic draft preparation.
- Ozon auto-publish до каждого нового write атомарно claim-ит item только пока exact run остаётся `running`, а settings enabled и не paused. Cancel/pause/disable, зафиксированный первым, делает provider call невозможным; если submit boundary уже пересечена, run остаётся `cancelling` до operation reconciliation и не выдаётся за отменённый upstream write. После restart item без boundary откладывается, а committed idempotency связывается с durable operation до любого безопасного продолжения.
- File claim координирует процессы только на общем host/filesystem. Текущий Compose имеет singleton scheduler и один web container; перед multi-host/web-replica rollout P11 обязан заменить claim распределённой блокировкой либо гарантировать shared lock filesystem.

### Единый AI-помощник

- `routes/agents.py`: seller-scoped chat, run, cancel, rollback и proposal review endpoints.
- `services/agent_harness.py`: conversations/messages, plan confirmation, task tree, checkpoints, proposals и rollback orchestration.
- `services/agent_wb_content.py`: seller-scoped публикация точного content diff из завершённого chat-run в один WB batch без повторной генерации.
- `services/agent_service.py`: очередь и lifecycle `AgentTask`.
- `routes/internal_api.py`: аутентифицированный API между runtime и платформой; здесь enforced tenant scope, protected fields и snapshots.
- `agents/runner.py`: CLI и registry. Имя `orchestrator` запускает `UnifiedSellerAgent`.
- `agents/unified.py`: semantic planner и in-process skill orchestration.
- `agents/base_agent.py`: ReAct loop, batches, cancellation, checkpoints, limits и usage aggregation.
- `agents/llm.py`: Claude, Gemini и OpenAI-compatible providers, включая native DeepSeek profiles.
- `agents/tools.py`: schema и registry доступных агенту tools.
- `services/agent_knowledge.py`: curated ingestion, tenant-aware FTS5/prefix/trigram retrieval, bounded context и offline evaluation для RAG.
- `scripts/manage_agent_knowledge.py`: административный CLI для версий документов, retrieval smoke-test и Recall@K/MRR evaluation.
- `agents/catalog/`: внутренние domain skills и pipeline catalog. Это не отдельные seller-facing агенты.
- `static/agent-chat.*`, `static/ai-chat-popup.*`, `templates/agents.html`: основной чат и компактный popup.

Основной поток: browser chat -> `routes/agents.py` -> `agent_harness` -> `AgentTask` -> poll единого orchestrator -> `UnifiedSellerAgent` -> internal skill -> tools -> authenticated internal API -> DB -> conversation polling -> UI. Точные полнофразные read-intents и safety-boundaries могут иметь deterministic fast-path, но regex/keyword никогда не является границей понимания: любой miss, опечатка, разговорная или составная фраза получает один bounded structured semantic plan или конкретный clarification. Planner видит текущий запрос, typed scope без списка ID, нормализованный page context, до 12 последних языковых/run-реплик общим объёмом до 6000 символов и bounded durable state последнего plan/run/clarification. UI явно передаёт `scope_mode=selected|global|page`; повторно присланные те же IDs считаются conversation scope, а planner возвращает `scope_mode=active|global`, чтобы опечатка вроде «весь коталог» не применила старую выборку. Когда пользователь пишет точный seller-owned WB `nmID`/числовой артикул прямо в тексте без UI selection, harness делает только tenant-scoped exact grounding в `Product`/`ImportedProduct` и передаёт найденный внутренний ID semantic planner как `scope_origin=message_reference`; это не intent-классификация. Неизвестная, смешанная или неоднозначная явно помеченная ссылка возвращает clarification и никогда не превращается в global write. Global write не расширяется из старого scope без явного подтверждения. Read-only semantic plan стартует автоматически, write-plan требует подтверждения. Явные вопросы к инструкциям идут в `knowledge-query`: task-scoped internal retrieval выбирает только global + документы текущего seller, после чего bounded Flash синтезирует ответ с проверенными citations; при отсутствии результата или бюджета возвращаются детерминированные cited excerpts без догадок.

Частые read-intents (цены, остатки, пропуски контента, import/publication status, supplier publication counts, WB catalog counts, API health, defaults, stop-words и pricing settings) обязаны сначала проходить строгий локальный parser и typed SQL/internal endpoint. Не используйте LLM как классификатор там, где intent и параметры можно строго проверить regex/enum; при любом miss, опечатке, лишнем модификаторе или составной цели fast-path обязан отказаться от решения и передать текст semantic planner. Generic count/list fast-path принимает только целую фразу без неизвестных модификаторов: «покажи просевшие карточки» нельзя молча превращать в выдачу всего каталога. Для deterministic catalog query допускается один короткий Flash-вызов только для формулировки ответа; в него передаются condition/count/has_results, но не карточки и не история диалога. Точные supplier counts возвращаются без polish-вызова.

Явно выбранные карточки обрабатываются typed batch-путём. `batch-audit` получает до 200 IDs одним tenant-scoped query и работает без LLM. Контентный write принимает до 100 IDs, один раз загружает compact content brief, затем использует bounded Flash chunks и пакетные writes. Любой write batch до SELECT/snapshot строго проверяет array объектов и уникальные positive integer IDs; bool, float, loose string и дубли отклоняют весь request. Не подменяйте этот путь циклом GET/PATCH или отдельным LLM-вызовом на карточку.

В tool-assisted batch модель получает только read/reference tools и возвращает typed `results`; все write tools из её allowlist удаляются. Python harness до LLM отклоняет дубли и неполный prefetch, проверяет принадлежность `product_id` текущему чанку и выполняет не более одного `batch_update_imported_products` на чанк. В prompt попадают только category/schema scopes текущего чанка. Чанк без tools имеет hard cap 1 LLM request, с reference tools — 4; общий run budget остаётся верхней границей. При положительном token budget один tool-batch chunk резервирует минимум 6000 токенов, поэтому default 30000 запускает не более пяти чанков, а хвост честно возвращается как deferred/failed. Не доверяйте model-reported `processed/saved` и не возвращайте сохранение под контроль prompt compliance.

Structured batch (brand/SEO) также является Python-owned write path. Выбранные IDs и prefetch обязаны совпасть exact-set до LLM; raw model `results` до postprocess содержит каждый ID текущего чанка ровно один раз, без чужих и дублей. После mapper update IDs могут быть unique subset чанка, но `failed` считается как `chunk_size - confirmed_saved`, включая пропущенные updates и `updated=0` без error rows. API/token-truncated хвост учитывается как deferred. Run token allocation резервирует практическую оценку повторяемого input (`ceil(utf8_bytes/2) + 256`) и функциональный structured output до вызова; output cap не превышает `LLM_MAX_TOKENS`. Для structured output резерв равен минимум 64 и 128 токенов на карточку, ограниченным provider cap. ReAct перед каждым model call повторно вычитает оценку полного `system + messages + tool schemas`; если input и хотя бы один output token не помещаются в chunk share, вызов не выполняется.

Раздел «Качество карточек» (`routes/card_quality.py`, `services/card_quality_scorer.py`,
`services/subject_charcs_cache.py`) не вызывает legacy-агентов. Quality Score v2 —
детерминированный: контент относительно конфига категории WB (кэш
`wb_subject_charcs_cache`, TTL 7 дней) плюс метрики воронки продаж, которые парсятся
из того же ответа sales-funnel, что и рейтинги (без дополнительных API-вызовов).
Причины «требует внимания» хранятся CSV в `Product.attention_reasons`
(коды в `card_quality_scorer.ATTENTION_REASONS`), приоритет — `Product.quality_impact`.
Кнопка «Исправить с ИИ» передаёт выбранные карточки в единый чат только через
существующие endpoints (`POST /agents/api/conversations`, `.../messages` с
`entity_kind='product'` + `product_ids` + `scope_mode='selected'`, лимит 50); write-путь остаётся
план → подтверждение → proposal. Рантайм читает quality-данные через
read-only internal endpoint `products/quality-brief` (agent-auth, до 50
карточек, protected fields не возвращаются): детерминированный skill
`quality-audit` агрегирует причины и отдаёт приоритетные карточки как
collection (`selected_product_ids` + `entity_kind='product'`), tool
`get_card_quality` доступен ReAct-skills только через allowlist. Явная выборка
`product_ids` в quality-brief не обрезается дефолтным limit; `quality-audit`
входит в `_CHAINING_SOURCE_SKILLS` и передаёт `selected_product_ids` следующему
шагу плана.

### Фотостудия и инфографика

- `routes/image_lab.py`, `templates/image_lab.html`, `static/image-lab.js` —
  seller-scoped лаборатория `/image-lab`: продавец видит до 10 нормализованных
  фото карточки и выбирает режим `single` (один ракурс), `each` (отдельная job
  на каждый выбранный ракурс), `reference_set` (одно главное фото в финале,
  остальные — identity references с ролями `angle|packaging|detail`),
  `collage` (локальный общий макет из 2–10 оригинальных foreground) или
  `angles` (research-only синтез отдельных `front|back|left|right|three_quarter_*|top`
  видов из 1–10 фото одного SKU). Один prompt запускается до трёх
  раз через GPU/Gen-API/AITunnel, результаты сравниваются вслепую,
  оцениваются 1–5 и тегами. `ImageGenerationExperiment` хранит воспроизводимые
  параметры, `generation_strategy`, `composition_mode`, главное фото, точные
  indices/roles, `requested_view`, watermark/text configs, стоимость, latency, quality JSON и
  локальные artifacts; миграции — `migrations/migrate_add_image_generation_lab.py`
  `migrations/migrate_add_image_lab_reference_watermark.py` и
  `migrations/migrate_add_image_lab_angle_synthesis.py`. Endpoint preview не отдаёт
  upstream URL в браузер: он tenant-scoped и последовательно пробует cache и
  `sexoptovik/blur/processed/original`, потому что original CDN может быть
  временно недоступен backend-контейнеру.
- `services/image_lab_service.py` владеет allowlist backend/model, prompt
  policy, SSRF-защитой загрузки исходника, seller budgets (active/24h/рубли),
  lifecycle и аналитикой. Browser никогда не получает provider/GPU secrets.
  API берёт seller только из `current_user.seller`; experiment/product/artifact
  всегда выбираются составным `id + seller_id`.
- Default `background_only` boundary: модель получает только описание пустой
  сцены, после чего оригинальный RGB товара накладывается локально ровно один
  раз. `reference_guided` передаёт Gen-API/AITunnel локально подготовленный 3:4
  canvas с protection mask, а `native_scene` — байты исходного главного фото без
  mask; в обоих edit-режимах provider output используется напрямую без второго
  foreground-слоя, всегда имеет `identity_mode=generative_edit`,
  `publishable=false` и требует human identity/duplicate review. В
  `reference_set` остальные выбранные байты идут отдельными identity references
  в порядке сохранённого manifest; `packaging`/`detail` являются только evidence
  и не должны появляться лишним объектом. `native_scene` недоступен для
  `collage`; `background_only` остаётся единственным режимом GPU bridge. Bounded
  visual context выбирается между ImportedProduct и
  более полным `SupplierProduct.ai_parsed_data_json`, удаляет цены/ID/instructions,
  ограничен размером и сохраняется в prompt. Пользовательский additional prompt
  не может отменить identity/no-duplicate/no-generated-text правила.
- `angle_synthesis` доступен только reference-capable Gen-API/AITunnel моделям:
  главное фото с ролью `angle` передаётся первым, остальные выбранные фото —
  отдельными evidence references с сохранённым manifest; на каждый выбранный
  `requested_view` создаётся самостоятельная job. Этот поток перерисовывает весь
  товар и синтезирует скрытую геометрию, поэтому использует
  `identity_mode=generative_edit`, всегда имеет `publishable=false` и требует
  human identity/geometry review. Никогда не переносите в него гарантию
  original RGB из `background_only` и не разрешайте auto-publish по rating.
- Gen-API `image_urls` имеет контракт `files_array`: локальные референсы Flux 2
  отправляются как повторяющиеся multipart-поля `image_urls[]`, не JSON data URI.
  AITunnel `gpt-image-2` поддерживает `reference_guided|native_scene` через
  `/v1/images/edits`, `image[]` и `input_fidelity=high`; protection mask
  передаётся только для `reference_guided`. Для
  вертикального запроса используется поддерживаемый provider size 1024×1536,
  после чего локальный финал нормализуется до 900×1200.
- После provider response `services/infographic_quality.py` локально накладывает
  foreground только для `background_only`: alpha берётся из rembg, RGB — строго
  из декодированного оригинала, разрешены только resize/translate. Нельзя
  одновременно передавать товар модели и затем добавлять тот же foreground в
  финал: смещение provider output создаёт дубликат. В `collage` каждый foreground
  имеет отдельный source hash/alpha metadata. Финал всегда 900×1200;
  `reference_guided|native_scene|angle_synthesis` остаются `review_required` до
  human/CV проверки identity, геометрии и лишних объектов и никогда
  автоматически не публикуются.
- Пользовательский текст передаётся модели только как layout intent для верхней
  safe-zone; точные UTF-8 glyphs рендерятся локально deterministic overlay и
  сохраняются в quality metadata. Seller-scoped PNG до 2 МБ нормализуется и
  накладывается локально с проверенными position/scale/opacity на каждый финал
  текущего запуска; логотип не отправляется модели и оригинальные фото не меняются.
- AI-фон проходит OCR no-text gate. Отсутствующий OCR не считается успехом:
  лаборатория возвращает `review_required`, а production renderer инфографики
  подменяет непроверенный/текстовый AI-фон безопасным детерминированным фоном.
  `auto_pass` требует decode, точный размер, visual signal, foreground metadata,
  отдельно подтверждённую alpha-mask (`mask_verified=true`), no-text background,
  отдельно подтверждённую сцену без людей/лишних предметов и с пустой зоной,
  точный deterministic overlay и fact-safe claims. Автоматическая rembg-mask
  имеет `automated_unreviewed` и сама по себе даёт только `review_required`:
  сохранность RGB не доказывает, что край товара не был обрезан маской.
  Negative prompt не считается scene verification: AI-background без отдельной
  CV/human проверки остаётся `review_required` даже при чистом OCR.
  Осмысленная alpha-mask, уже находящаяся в исходном PNG, считается частью
  source-of-truth (`source_alpha`) и не прогоняется через rembg.
- `services/infographic_content.py` строит слайды без LLM из bounded fact pack.
  Каждый видимый title/subtitle имеет `fact_id + source` и совпадает с фактом
  дословно; filler-слайды и неподтверждённые `ХИТ/ТОП/ЛУЧШИЙ/НОВИНКА` запрещены.
  `SupplierService.ai_generate_rich_content` сохраняет legacy API-имя, но модель
  больше не вызывает. Старый rich content без fact-safe contract не рендерится.
- `services/infographic_renderer.py` генерирует background-only, композитит
  оригинальный foreground и лишь затем кладёт HTML-текст в верхнюю safe-zone,
  не накрывающую товар. Provider failure даёт deterministic template, а не
  генеративную замену товара. В output возвращаются quality/publishable.
- GPU tunnel: `scripts/gpu_pilot/http_bridge.py` (Bearer token, localhost по
  умолчанию, HTTPS/SSH/WireGuard снаружи) пишет background-only jobs в очередь
  `qwen_worker.py`. `edit/posters` требуют `research_only=true`.
  `finalize_backgrounds.py` локально собирает и оценивает raw GPU backgrounds.
- Docker image использует системный `/usr/bin/chromium` для Playwright вместо
  загрузки browser bundle с Playwright CDN, `fonts-dejavu-core` для точного
  кириллического overlay; rembg-модель прогревается при build.
- Лимиты/секреты: `GEN_API_KEY`, `AITUNNEL_API_KEY`, `GPU_IMAGE_SERVER_URL`,
  `GPU_IMAGE_SERVER_TOKEN`, `GPU_IMAGE_ALLOW_HTTP`, `GPU_IMAGE_STEPS`,
  `GPU_IMAGE_TRUE_CFG`, `GPU_IMAGE_RUB_PER_GENERATION`,
  `IMAGE_LAB_MAX_ACTIVE_JOBS`,
  `IMAGE_LAB_DAILY_JOB_LIMIT`, `IMAGE_LAB_DAILY_BUDGET_RUB`,
  `IMAGE_LAB_PROVIDER_TIMEOUT`, `IMAGE_LAB_DATA_DIR`, `IMAGE_LAB_INLINE_WORKER`,
  `INFOGRAPHIC_REMBG_MODEL`; GPU host использует `GPU_BRIDGE_TOKEN`.
  Полный runbook и честная интерпретация исторического пилота —
  `docs/INFOGRAPHICS_PILOT.md`.

Локально `IMAGE_LAB_INLINE_WORKER=1` выполняет jobs bounded executor'ом web
процесса. `IMAGE_LAB_MAX_ACTIVE_JOBS` ограничивает только реально выполняемые
`running|remote_running|finalizing`; multi-photo batch может безопасно лежать в
`queued`, а polling повторно предлагает queued job атомарному claim. Суточные
лимит и бюджет учитывают все созданные jobs. В production установите
`IMAGE_LAB_INLINE_WORKER=0` для web и
запустите `SKIP_SCHEDULER=1 python scripts/run_image_lab_worker.py`; claim
`queued -> running` атомарный, поэтому повторный runner не дублирует запрос.

## Локальный запуск

Требуется Python 3.11. Проект не имеет Makefile, `pyproject.toml`, `package.json` или frontend build step.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
SKIP_SCHEDULER=1 python scripts/init_platform.py
DISABLE_SECURE_COOKIE=1 PORT=5001 python seller_platform.py
```

Откройте `http://localhost:5001/login`. `DISABLE_SECURE_COOKIE=1` допустим только для локального HTTP. Без `DATABASE_URL` основная база создаётся в `data/seller_platform.db`.

Ozon account/catalog/draft/commercial/quality/analytics/fulfillment/finance/inbox UI остаётся выключенным по умолчанию. Для локальной проверки сначала задайте валидный Fernet `ENCRYPTION_KEY`, затем явно экспортируйте `MARKETPLACE_OZON_ENABLED=1`. Seller проверяет кабинет в `/marketplaces/accounts/`, запускает bounded read-only catalog sweep в `/marketplaces/listings`, готовит validated drafts в `/marketplaces/drafts/`, синхронизирует склады/создаёт read-only proposals в `/marketplaces/commercial/`, смотрит отдельные Ozon-оценки в `/marketplaces/quality`, read-only аналитику в `/marketplaces/analytics`, заказы в `/marketplaces/orders`, возвраты в `/marketplaces/returns`, отмены в `/marketplaces/cancellations`, immutable accrual snapshots в `/marketplaces/finance` и capability-gated отзывы/вопросы в `/marketplaces/reviews`. Draft validation и quality recompute не вызывают Ozon или LLM; analytics/fulfillment/finance/inbox refresh вызывает только manifest endpoints с retry class `read`. Reply draft может сделать один bounded seller-scoped AI-вызов, но остаётся локальным. Ручные create/full-update/product rollback независимо включаются `MARKETPLACE_OZON_PUBLICATION_ENABLED=1`; auto-publish требует ещё `MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=1` и включения точного account scope на `/auto-publish`; approve price/stock proposal использует `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=1`. Операции и reconciliation видны в `/marketplaces/operations/`. Никогда не включайте write flags только ради unit tests: они используют synthetic credentials/adapters и не вызывают Ozon.

Для точечной сверки live read-контрактов вне web runtime создайте `/tmp/ozon_live.env` с правами `0600` и только ключами `OZON_LIVE_CLIENT_ID`/`OZON_LIVE_API_KEY`, затем выполните `python scripts/probe_ozon_read_contracts.py`. Не кладите эти имена/значения в project `.env`: probe не загружает shell-файл и парсит только два exact key без eval. Скрипт не имеет write mode, перед каждым вызовом проверяет `OZON_ENDPOINTS[endpoint].retry_class == 'read'` и печатает только структуру ответа. Raw live body не сохраняйте в fixtures; переносите только synthetic/redacted форму контракта.

Для non-interactive init передайте `ADMIN_USERNAME`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` через окружение и выполните:

```bash
SKIP_SCHEDULER=1 python scripts/init_platform.py --non-interactive --skip-existing
```

Не вставляйте реальные пароли, API keys, encryption keys или содержимое `.env` в код, тесты, логи и документацию. `.env.example` перечисляет только допустимые имена настроек. Корневой web runtime не загружает `.env` автоматически при прямом `python seller_platform.py`; экспортируйте нужные значения в shell.

Локальный unified runtime запускается после активации orchestrator в UI и настройки seller AI profile:

```bash
PLATFORM_URL=http://127.0.0.1:5001 \
AGENT_ID='<orchestrator-id>' \
AGENT_API_KEY='<orchestrator-key>' \
python -m agents.runner --agent orchestrator --log-level INFO
```

Проверка registry без запуска worker:

```bash
python -m agents.runner --list
```

Калькулятор при необходимости запускается отдельно:

```bash
DISABLE_SECURE_COOKIE=1 PORT=5000 python app.py
```

## Docker Compose

Текущий Compose публикует только Caddy на `80/443`. `seller-platform:5001` доступен лишь внутри Docker network.

```bash
cp .env.example .env
# Заполните обязательные SECRET_KEY и ADMIN_PASSWORD; ENCRYPTION_KEY настоятельно рекомендуется.
docker compose up -d --build
docker compose ps
docker compose logs -f seller-platform
```

Откройте `https://localhost/login` или домен из `DOMAIN`. Entry point сам создаёт/обновляет администратора из env, применяет миграции, включает SQLite WAL и запускает HTTPS Gunicorn.

Unified agent удобнее подключать вторым этапом:

1. Поднимите web stack.
2. Активируйте orchestrator на странице `/agents`.
3. Поместите выданные значения в `AGENT_ORCHESTRATOR_ID` и `AGENT_ORCHESTRATOR_KEY` локального `.env`.
4. Запустите runtime:

```bash
docker compose --profile agents up -d --build agent-orchestrator
docker compose logs -f agent-orchestrator
```

Полный уже настроенный стек:

```bash
docker compose --profile agents up -d --build
```

Для отдельного durable worker Фотостудии установите
`IMAGE_LAB_INLINE_WORKER=0` и запустите:

```bash
docker compose --profile image-lab up -d --build image-lab-worker
docker compose logs -f image-lab-worker
```

Profile `legacy-agents` поднимает старые специализированные workers и не является рекомендуемым runtime. Не добавляйте туда новые seller-facing возможности без отдельного migration plan.

Данные платформы находятся в named volume `seller_platform_data`; `uploads/` и `processed/` являются bind mounts. `docker compose down` сохраняет volume, а `docker compose down -v` удаляет его и считается разрушительной операцией.

## Тесты и проверки

`pytest` не входит в runtime `requirements.txt`. Установите его в dev environment отдельно:

```bash
python -m pip install pytest
```

Основная команда:

```bash
SKIP_SCHEDULER=1 python -m pytest -q
```

Узкий набор для unified AI:

```bash
SKIP_SCHEDULER=1 python -m pytest -q \
  tests/test_agent_harness.py \
  tests/test_internal_agent_security.py \
  tests/test_base_agent.py \
  tests/test_llm.py \
  tests/test_unified_usage.py
```

Для отдельного `unittest.TestCase` файла допустим запуск вида:

```bash
SKIP_SCHEDULER=1 python -m unittest tests.test_agent_harness
```

Минимум перед завершением Python-изменения:

```bash
python -m py_compile path/to/changed_file.py
git diff --check
```

Добавляйте тест рядом с изменённым контрактом. Для route/service изменений проверяйте success, authorization/tenant denial, validation failure и rollback. Для агента проверяйте tool allowlist, partial/budget exits, protected fields и usage. Не выполняйте реальные WB/LLM запросы в unit tests.

## База данных и миграции

Docker entrypoint является наиболее полным migration path: `db.create_all()`, startup migrations и набор scripts из `migrations/`, включая unified chat.

Перед ручной миграцией остановите writers и сделайте backup. Для локальной базы доступны:

```bash
python migrations/migrate_add_agent_chat.py data/seller_platform.db
python migrations/migrate_add_marketplace_commercial.py data/seller_platform.db
python migrations/migrate_add_marketplace_product_updates.py data/seller_platform.db
python migrations/migrate_add_image_generation_lab.py data/seller_platform.db
python migrations/migrate_add_image_lab_reference_watermark.py data/seller_platform.db
python migrations/migrate_add_image_lab_angle_synthesis.py data/seller_platform.db
python migrations/migrate_add_wb_dictionary_provenance.py data/seller_platform.db
python migrations/migrate_add_marketplace_accounts.py data/seller_platform.db
python migrations/migrate_add_ozon_references.py data/seller_platform.db
python migrations/migrate_add_marketplace_listings.py data/seller_platform.db
python migrations/migrate_add_marketplace_product_links.py data/seller_platform.db
python migrations/migrate_add_marketplace_drafts.py data/seller_platform.db
python migrations/migrate_add_marketplace_operations.py data/seller_platform.db
python migrations/migrate_add_marketplace_auto_publish.py data/seller_platform.db
python migrations/migrate_add_marketplace_quality_analytics.py data/seller_platform.db
python migrations/migrate_add_marketplace_fulfillment.py data/seller_platform.db
python migrations/migrate_add_marketplace_finance.py data/seller_platform.db
python migrations/migrate_add_marketplace_inbox.py data/seller_platform.db
python migrations/run_all_migrations.py data/seller_platform.db
```

Правила schema changes:

- Создавайте новый идемпотентный script в `migrations/`; не полагайтесь только на `db.create_all()` для существующих БД.
- Не переписывайте уже развёрнутую миграцию так, чтобы старые инсталляции получили другое поведение.
- Миграции, добавляющие колонки, которые ORM читает сразу после старта, должны быть fail-fast в `docker-entrypoint.sh`; не скрывайте их ошибку через `|| echo`.
- `ImportedProduct` — текущая каноническая seller-owned карточка и единственный источник общего контента/AI-кэша. `Product` остаётся WB projection, `MarketplaceListing` — account/channel projection. Ozon catalog sync может auto-link только одно уникальное exact offer/vendor совпадение; title similarity и LLM не создают связь. Ambiguous остаётся unlinked до seller confirmation; link/unlink требуют tenant scope, optimistic `link_version` и append-only event. Одна внутренняя карточка не может быть связана с двумя listings одного account.
- Подключайте новый script к `docker-entrypoint.sh` и, когда уместно, к comprehensive migration path.
- Держите DDL и backfill повторно запускаемыми; проверяйте наличие table/column/index.
- Backfill обязан явно заполнять Python-side default поля вроде `created_at`: таблица, созданная SQLAlchemy, может не иметь server default даже если новый migration DDL его объявляет.
- Marketplace auto-publish снимает legacy physical `UNIQUE(seller_id)`, поэтому его schema нельзя обновлять набором `ADD COLUMN`. `migrate_add_marketplace_auto_publish.py` делает idempotent transactional rebuild, сохраняет WB rows как `account_id=NULL`, проверяет foreign keys и запускается fail-fast в Docker/comprehensive/direct SQLite startup paths.
- Не удаляйте таблицы, volume или пользовательские данные без явного запроса и проверенного backup/restore plan.

## Safety-инварианты

Эти правила нельзя ослаблять ради удобства реализации.

### Tenant scope

- Любой user-facing read/write должен исходить из `current_user.seller`, а не доверять `seller_id` из body/query.
- Marketplace account всегда выбирается составным `account_id + current_user.seller.id`; `Client-Id` не является tenant scope. Adapter вызывается только после этой проверки.
- Seller marketplace credentials шифруются только валидным `ENCRYPTION_KEY`, никогда не возвращаются в public serializer и не сохраняются из response/error text. Global reference credentials и operational seller accounts не подменяют друг друга.
- Ozon global reference route доступен только admin и только при feature flag; удаление reference secret остаётся доступным при rollback flag. Reference account не разрешено использовать для seller catalog, prices, stocks или публикации.
- `MarketplaceListing`, catalog sync run и account всегда читаются/изменяются с тем же `seller_id`; фильтр по голому listing/account ID запрещён. Enrichment response не может добавить чужой `product_id` или конфликтующий `offer_id` в запрошенный page exact-set.
- `MarketplaceProductDraft`, его `ImportedProduct`, account, marketplace и category mapping обязаны иметь один seller/marketplace scope. `corrected_by_user_id` берётся из authenticated user и повторно сверяется с `Seller.user_id`; body не может назначить автора исправления.
- `MarketplaceOperation` и snapshot всегда выбираются с `operation.id + current_user.seller.id`; их account/draft/listing scope повторно сверяется. Public serializer не отдаёт `idempotency_key`, submitted state или credentials.
- Auto-publish settings/run/item user routes всегда выбирают selector scope из query, затем exact `settings_id + seller_id + account_id`; body не может сменить marketplace/account. WB row обязан иметь `account_id=NULL`, Ozon row — seller-owned Ozon account. Публичный item serializer не отдаёт внутренний idempotency key.
- Ozon analytics sync/fact/quality, fulfillment posting/item/status/return/cancellation, finance snapshot/fact/component и inbox sync/item/draft rows всегда выбираются по полному `seller_id + marketplace_id + account_id`; bare sync/posting/listing/account/item/fact ID недостаточен. `account_id` для insight/fulfillment/finance/inbox routes задаётся только canonical positive query-параметром, duplicate/unknown query и body scope smuggling запрещены. `created_by_user_id` reply draft повторно сверяется с `Seller.user_id`.
- Любой internal agent request обязан пройти agent authentication, task ownership и assignment-to-seller checks.
- Запрос объекта выполняйте составным условием `id + seller_id`; проверка одного ID недостаточна.
- Область сущности всегда типизирована: `/products/<id>` означает `Product`, а страницы импорта/поставщика — `ImportedProduct`; числовой ID без `entity_kind` неоднозначен.
- `marketplace_listing` дополнительно всегда содержит точный `marketplace_code + account_id`; bare listing ID, numeric string, bool/float, duplicate, mixed-account или foreign-seller selection отклоняются целиком до чтения/LLM. Internal marketplace tool обязан сверить тот же exact-set с active assigned task, а ответ строится только из allowlisted локальных facts без raw provider blobs/credentials.
- Popup conversation хранится по fingerprint текущей страницы и entity IDs. Не переиспользуйте один session key между разными карточками; backend также отклоняет смену typed product scope внутри page-context диалога.
- Popup не рендерится на `/admin`: административные данные и справочники доступны агенту только через typed least-privilege tools, а не через DOM/page context.
- Parent/subtask, conversation, proposal, snapshot и rollback должны принадлежать тому же seller.
- Админские исключения должны быть явными и покрыты тестом. Не возвращайте credentials или внутренние encrypted fields.

### Price и stock только через proposal

- Агент не применяет price/stock fields напрямую.
- `routes/internal_api.py` должен удалять protected fields из update payload и создавать `AgentReviewProposal`.
- Для основной `Product`, пока proposal target поддерживает только `ImportedProduct`, protected payload отклоняется целиком с `requires_manual_review=true`; silent `ok` запрещён.
- Изменения применяются только после явного human review через seller-scoped endpoint; pricing guardrails и минимальная маржа остаются обязательными.
- Не расширяйте writable allowlist ценами, остатками или их alias. Не обходите proposal path из нового tool/service.

### Записи, snapshots и rollback

- До изменения карточки сохраняйте `AgentChangeSnapshot` с previous/new values и task/agent identity.
- Batch updates должны изолировать ошибки через transaction/savepoint и не оставлять session в failed state.
- Write workflow должен иметь понятный rollback path. Rollback также tenant-scoped и идемпотентен.
- Ozon auto-publish не имеет права менять `ImportedProduct.import_status`: это WB projection. Его side effect существует только как `MarketplaceOperation`/snapshot. Quota/daily tail получает `deferred`, ambiguous/claimed operation остаётся на reconciliation, а cancellation останавливает только ещё не claimed writes.
- Для `ImportedProduct` используйте `AgentChangeSnapshot`; для основной `Product` — `CardEditHistory` с `user_comment=agent_task:<task_id>`. Локальный rollback `Product` применяет snapshot только если текущие changed fields точно равны `snapshot_after`; поздняя ручная правка даёт per-card `conflict` и не перезаписывается. Оба пути должны тестироваться полным циклом запись → откат.
- `content-writer` меняет только локальную `Product` и создаёт проверяемый diff. Отправка в WB является отдельным подтверждаемым write-plan `wb-content-publisher`: он берёт точный latest completed diff тех же fields из того же seller-scoped conversation, повторно сверяет локальные значения и посылает один typed WB batch без LLM и регенерации. Перед network I/O history атомарно claim-ится условным `pending|failed -> uncertain` с task-specific marker, поэтому два worker не могут отправить один diff; строка сразу исключается из local rollback. Transport timeout/5xx после возможной отправки и неполное accounting не ретраятся вслепую, confirmed success не откатывается только локально, а явный отказ WB или доказанный pre-write GET/DNS failure оставляет локальный diff доступным для conflict-aware rollback.
- Rollback task tree выполняйте только после остановки его активных задач, иначе поздний worker может повторно записать данные.
- Не запускайте destructive workflow из неоднозначного текста: semantic planner возвращает clarification или план, а пользователь подтверждает write-plan до старта.

### Least privilege и достоверность

- Skill получает минимальный `tool_allowlist`; read-only skill не должен иметь update tools.
- LLM не является источником истины для seller/product identity, разрешений, цен, остатков, сертификатов, состава и иных непереданных фактов.
- Сохраняйте inference policy и confidence. Validate structured output и tool arguments перед side effects.
- Не логируйте chain-of-thought, секреты и полные sensitive payloads. UI показывает проверяемые шаги и результаты.

### Характеристики WB из данных поставщика

- Любой supplier/AI characteristic patch перед созданием или обновлением карточки проходит fail-closed проверку по WB category schema из `MarketplaceCategoryCharacteristic` и тому же effective constraint resolver, который видит AI. Явный non-empty category `dictionary_json` из admin или WB schema является строгим allowlist. `Цвет`, `Пол|Пол товара`, `Страна производства`, `Сезон` и НДС используют официальные глобальные `MarketplaceDirectory` (`colors|kinds|countries|seasons|vat`) и не подменяются старым category-списком. ТНВЭД загружается из официального category-scoped endpoint только с typed `subjectID` и хранит `isKiz` в dictionary snapshot.
- WB schema не публикует универсальный allowlist для каждой строковой характеристики. Поэтому `Материал изделия` и `Состав|Состав изделия|Состав материала` без explicit admin/WB dictionary являются free-text, а не «отсутствующим обязательным словарём». Не вводите dictionary requirement по имени поля. Если админ явно задал policy allowlist, он остаётся строгим и не стирается пустым WB response.
- Отсутствующая/устаревшая категория, схема или реально constrained-словарь блокирует отправку характеристик в WB с диагностикой о синхронизации. Free-text без explicit dictionary остаётся usable. Нельзя молча пропускать невалидное constrained-значение и сообщать об успешном обновлении.
- В apply-path допустима только точная case-insensitive канонизация значения из полного effective-словаря. Fuzzy/substring matching нельзя автоматически применять перед side effect. Для большого truncated-словаря AI использует read-only batch tool `search_characteristic_values`; tool может ранжировать кандидаты, но в write result попадает только дословная каноническая строка из его `values`.
- Обновление характеристик является patch по `charc_id`: проверенные канонические значения мержатся с полным текущим массивом, чтобы частичное обновление не удалило остальные характеристики. Повреждённый текущий JSON блокирует запись; snapshot/history хранит именно фактически записанный merged JSON.
- Центральные single/create/batch методы WB API повторно проверяют characteristic patch и все уже находящиеся в full-card известные dictionary-bound значения. `subjectID`, patch `id` и ID удаляемых при rollback характеристик принимаются только как positive JSON integer: boolean, float и numeric string не канонизируются через `int(...)`. Batch принимает только full-card с opaque fetched-source receipt, подготовленную поверх свежей карточки WB: prepared context привязан к `nmID`, typed `subjectID`, ID изменённых/удалённых характеристик и fingerprint финального массива, а перед HTTP удаляется. Raw batch, перенос контекста между карточками и неявная потеря untouched-характеристик при нормализации запрещены. `null`/объект не может заменить массив, а пустой patch не очищает существующие значения. Supplier flow до HTTP коммитит durable pending `CardEditHistory` с точными fresh-before/sent-after; неоднозначный transport error сверяется с live WB, а неразрешённая `uncertain|pending`-запись становится доступна для conflict-aware rollback после 5 минут. Фото записываются отдельно и не отключают rollback контента. Rollback конфликтный, tenant-scoped и идемпотентный для одной или цепочки history-записей при повторе после сбоя локального commit. Supplier bulk принимает не более 200 уникальных positive integer Product IDs и переиспользует request-level schema/dictionary cache.
- Ручные category allowlists из админки не должны стираться, когда официальный WB schema response не содержит `dictionary`. Их изменение обновляет `dictionary_source=admin`, version/hash/time и schema hash. Rollback характеристик также повторно проходит актуальные schema/dictionary checks; устаревший или недопустимый snapshot не отправляется в WB.
- Не выводите пол, материал, вес или размеры из одной категории и не задавайте общий fallback `Пол=Унисекс`. Значение должно прийти из проверяемых данных и, если поле constrained, пройти effective-словарь WB/admin.

### Актуальность справочников WB

- Категории, характеристики и специальные справочники из админки являются structured truth. Агент читает их typed SQL/internal tools; не переносите эти данные в RAG, prompt constants или память модели. `MarketplaceCategoryCharacteristic` хранит `dictionary_source=none|admin|wb_schema|wb_directory`, `dictionary_synced_at`, stable hash и monotonically increasing version; миграция `migrate_add_wb_dictionary_provenance.py` помечает исторические non-empty списки как `admin`, потому что их upstream-происхождение нельзя доказать задним числом.
- Общий refresh категорий и глобальных справочников выполняется каждые 24 часа и через 90 секунд после старта scheduler. Для включённых категорий recovery refresh запускается через 180 секунд после старта с лимитом 200, затем refresh-ahead выбирает схемы старше 30 часов пакетами до 50 каждые 6 часов. Перед AI reference read и WB import/create batch `ensure_wb_references_current` делает bounded on-demand preflight для уникальных `subjectID`: свежий scope не вызывает WB, а stale/missing scope обновляется один раз на batch. Scheduler/on-demand/manual jobs не должны одновременно синхронизировать один schema batch. Hard TTL для agent read/write равен 48 часам: после него данные не используются до успешной синхронизации.
- Синхронизация применяет изменения только после полного типизированного upstream snapshot с успешным top-level WB envelope. Пустой/невалидный ответ, `error=true`, повтор страницы, дубли ID/значений или аномальное уменьшение snapshot не должны снимать availability либо затирать последний успешный кэш. Category `subjectID`/`parentID`, schema `subjectID`/`charcID`/`charcType`/`maxCount` принимаются только как JSON integer без coercion из boolean, float или string; category availability flags и обязательные schema flags `required`/`popular` принимаются только как JSON boolean. Опциональные schema flags `hasFilter`/`isVariable`/`existNamedField` также обязаны быть boolean при наличии, но их отсутствие в официальном ответе нормализуется в безопасный `false` для хранимых полей. Schema response обязан содержать точный запрошенный `subjectID` и typed characteristic fields; `colors`, `countries`, `kinds`, `seasons` и `vat` валидируются по своей официальной форме до записи. Необязательные display-метаданные WB вроде `colors.parentName` могут быть `null`/пустыми и нормализуются, но канонические ID/`name` остаются обязательными и строгими.
- Удалённые WB категории и характеристики не удаляются физически: помечайте их `is_available=false`, сохраняйте историю/настройки администратора и исключайте из новых agent choices. Обновляйте `last_seen_at`, status/error, version и hash; изменение имени, типа, обязательности, `hasFilter`, единицы, лимита или словаря считается новой версией схемы.
- Пользовательские `ai_instruction` не перезаписываются синхронизацией. Для этого хранится явный source `generated|custom`; автоматически регенерируются только generated instructions.
- WB `required=true` сильнее старого admin-флага `is_enabled`: ставшая обязательной характеристика автоматически включается обратно, всегда выдаётся агенту и не может быть отключена вручную.
- Internal reference endpoints возвращают `reference_status`. Категоризация, заполнение характеристик и нормализация размеров обязаны сделать zero-LLM preflight и вернуть проверяемый partial/clarification при `usable=false`. Схема характеристик usable только при свежем общем каталоге и `is_available + is_enabled + is_leaf` у категории. Write endpoints повторно валидируют freshness, availability, точные имена/ID, типы и словари, поэтому prompt-инструкция не является safety boundary.
- Schema endpoint отдаёт для каждой характеристики компактный `constraint` из того же validator-resolver: `source`, `constrained`, `usable`, `count`, не более 40 `values`, `truncated`, `dictionary_source`, `dictionary_synced_at` и `dictionary_version`. Весь global directory JSON в prompt не передаётся. Непроверяемое required-поле делает всю схему `usable=false` до LLM; optional-поле с `constraint.usable=false` пропускается.
- Batch prefetch категорий и схем использует authenticated `/internal/v1/categories/search-batch` и `/internal/v1/categories/characteristics-batch` с лимитом 1..200: один request делает bounded bulk SELECT, сохраняет порядок входа и возвращает typed fail-closed item для каждого query/`subjectID`, включая missing/stale/unavailable. Поиск в больших allowlists использует `/internal/v1/categories/characteristic-values/search-batch` с 1..50 уникальными typed queries и точным сохранением порядка.
- Batch write-path переиспользует request-level schema/directory validation cache. Не выполняйте повторный набор reference SELECT для каждой карточки одной категории.
- Scheduler и ручные category/directory/schema refresh используют общие non-blocking cross-process file claims: занятый scope возвращает `skipped` до изменения status или API-вызова. Stale-schema batch ставит повторно failed scopes после stale-success и untouched, чтобы постоянная ошибка с `synced_at=NULL` не создавала starvation.
- Соблюдайте актуальные WB Content API rate limits общим process-wide limiter и pacing. Не создавайте burst из одного запроса на каждую включённую категорию без bounded batch.
- Бренды WB загружаются по каждой включённой категории через актуальную cursor pagination `subjectId + next`, а не через fan-out `pattern`. Один запуск ограничен 200 запросами/страницами; полные category snapshots применяются вместе с `BrandCategoryLink`, а durable checkpoint продолжает sweep с первой незавершённой категории. Исчерпание budget, повтор cursor, неверный `total` или неполная категория не продвигают global freshness. Транспортная полнота сверяется по числу upstream items, а не только пригодных записей: элемент со строго типизированным положительным уникальным integer ID и пустым именем исключается из runtime-cache, но не делает весь snapshot неполным; невалидный/повторный ID блокирует категорию. Rename по стабильному WB brand ID до обновления binding создаёт либо переиспользует active exact `BrandAlias` того же бренда; manual/inactive alias или canonical-name conflict не перезаписывается и fail-closed блокирует rename/freshness. Пустой complete-response сохраняет последний успешный кэш и помечает sync как failed.
- Brand refresh запускается через 360 секунд после startup и затем каждые 6 часов, использует только центральный `Marketplace.api_key`. Незавершённый checkpoint возобновляется каждые 10 минут; без `status=partial + checkpoint` resume job не вызывает WB. Scheduler/manual runs сериализуются process-wide advisory lock `BRAND_SYNC_LOCK_FILE` (по умолчанию `/tmp/seller-platform-brand-sync.lock`); не запускайте обход без этого lock и не подменяйте credential случайным seller key. Недавний verified `BrandCategoryLink` является отдельным 48h category-scoped доказательством для агента, даже пока global sweep имеет status `partial`.
- Batch brand resolver проверяет до 100 пар `brand + category_id` одним `/internal/v1/brands/validate-batch` и bulk SQL. Не возвращайте N вызовов single validate и не записывайте бренд при missing/stale category evidence.
- `brand-resolver` до первого LLM-вызова делает typed `/internal/v1/brands/preflight` для 1..100 выбранных `subjectID` за запрос; unusable/stale scope или отсутствующая выбранная карточка возвращает zero-token blocked result. Boolean, float, строка с дробью и строка с ведущим нулём не являются integer ID и отклоняются без `int(...)`-усечения. Любой явно переданный `brand` в single/batch write для `Product` и `ImportedProduct`, включая строку `Нет бренда`, повторно проходит тот же exact bulk resolver, требует `verified + is_available` WB binding и свежий `BrandCategoryLink`, после чего сервер записывает только канонический `marketplace_brand_name`. Смена категории без явного brand patch не должна проверять старое сырое значение: следующий brand-step нормализует его отдельно.
- Structured brand chunk принимается только когда модель вернула ровно один объект для каждого ID текущего чанка. Дубликат, пропущенный или чужой `product_id`, не-object либо не-integer ID блокирует весь чанк до brand validation и до write; при параллельных чанках expected IDs хранятся thread-local.
- `MarketplaceBrand` доступен агенту только при одновременных `status=verified` и `is_available=true`. `pending`, `rejected` и `needs_review` остаются для админского review, но не попадают в runtime cache и internal brand validation как WB-binding.
- ТНВЭД является category-scoped справочником: `/content/v2/directory/tnved` требует `subjectID`. Не включайте его в глобальный `sync_directories`; кэш и tool для ТНВЭД должны сохранять typed category scope.

### Актуальность справочников Ozon

- Ozon tree принимается только как complete strict envelope с категориями и конечными типами. JSON boolean/integer не coercion-ятся; узел обязан иметь ровно один identity kind. Disabled ancestor делает всех потомков unavailable, даже если их собственный `disabled=false`.
- Attribute schema всегда выбирается точной локальной сущностью `MarketplaceProductType`; запрос строится из сохранённой пары category/type. Required schema fields принудительно enabled. Custom `ai_instruction_source=custom` и manual restriction не перезаписываются provider sync.
- Dictionary pagination должна быть строго возрастающей, без дублей, пустой промежуточной страницы и превышения page/item budgets. Ни одна value row не меняется до завершения полного sweep. `values_sync_checkpoint` хранит только диагностические page/cursor/fetched; resume с него запрещён без отдельной staging table.
- Usable reference требует last-good tree, schema и нужный dictionary не старше 48 часов. Ошибка нового refresh не удаляет last-good hash/timestamp; после hard TTL любой будущий mapping/publication обязан остановиться до успешной синхронизации.
- Admin allowlist хранит opaque value IDs и валидируется exact-set против fresh available rows того же attribute. Fuzzy match допустим только как suggestion; неизвестный, unavailable, duplicate или чужой scope ID не сохраняется.

### Полный read-snapshot каталога Ozon

- Product list page обязан иметь strict object envelope, bounded items, integer `total`, string cursor, уникальные product/offer identities и настоящие JSON boolean. Numeric string, float и boolean не превращаются в product ID через `int(...)`; на доменной границе provider ID хранится opaque string.
- `product/info/list`, `product/info/attributes`, prices и stocks запрашиваются batch-ом для IDs текущей list page. Foreign ID, conflicting offer, повтор ID между cursor pages той же phase, изменившийся total, пустая промежуточная страница или cursor loop отклоняют всю локальную page transaction. Durable `last_catalog_sync_phase` разрешает ожидаемое пересечение `ALL`/`ARCHIVED`, но не повтор внутри одной phase.
- В БД сохраняется только whitelist-normalized bounded snapshot: статусы, ошибки, атрибуты, complex attributes, media, dimensions, barcodes и price/stock summary. Не сохраняйте произвольный raw response и не считайте удалённое Ozon `marketing_price` обязательным полем.
- `ALL` и `ARCHIVED` могут пересекаться; dedup выполняется по account-scoped offer/product identities, а `seen_count` считает уникальные local listings run-а. Archived означает существующий upstream listing (`is_available=true`, `is_archived=true`), а не missing.
- Ошибка enrichment или неполный list sweep может обновить только уже полностью подтверждённые страницы. Она не имеет права пометить unseen rows unavailable. Final missing pass дополнительно не трогает rows, созданные после `run.started_at`, чтобы параллельная публикация не была ошибочно скрыта.
- WB backfill идемпотентен по `legacy_product_id`, не удаляет/не меняет `Product`, не требует seller marketplace account и использует стабильный fallback offer `wb-nm-<nm_id>` только когда `vendor_code` пуст.

### Ozon drafts, category mapping и AI facts

- `MarketplaceProductDraft` хранит нормализованное локальное состояние, но не считается `/v3/product/import` body. Даже `validation_status=valid` не разрешает route/tool side effect: P5 builder обязан повторно сверить fact hash, account, exact category/type, schema/dictionaries, quota и полный payload.
- Fact pack строится только из seller-owned `ImportedProduct` и bounded полей его supplier source. `original_data` physical/characteristic values имеют provenance `observed`; legacy `ai_parsed_data_json` и поля без доказуемого источника находятся только в `unverified_suggestions` и не auto-map-ятся.
- Full-product AI prompt использует policy `explicit_only`: отсутствующие значения возвращаются как `null`/`[]`, а каждый непустой факт должен иметь `parsing_meta.field_provenance`. Оценка веса/размеров/комплектации и default упаковка `20×20×30` удалены; fill percentage нельзя повышать догадками.
- Category mapping — точное seller-scoped подтверждение. Fuzzy/AI result может существовать только как `suggested`; автоматическое применение разрешено лишь для `active` mapping на enabled/available Ozon type. Смена типа очищает старые attributes/complex groups и требует новой validation.
- Draft update требует typed positive `expected_version`; boolean/float/numeric string в JSON не coercion-ятся. Source drift не перезаписывает user fields: validation возвращает `source_facts_stale`, а отдельный refresh меняет только fact snapshot/provenance.
- Dictionary value валидируется как exact `(product_type_id, attribute_id, external_value_id, display value)` по fresh official cache и optional admin restriction. Required/complex/max-count/type semantics проверяет Python; неизвестный data type блокирует draft. Значение другого category/type scope не переносится.
- Physical fields принимаются только как положительные явные values с units; price, VAT и `currency_code=RUB` для текущего rollout должны быть явными. Media принимает только bounded public HTTP(S) URLs; `images360` отклоняется как удалённое поле. `offer_id` обязателен и уникален внутри account.

### Ozon publication operations

- User route не вызывает Ozon напрямую. Create path: seller scope → fresh full draft validation → whitelist builder → committed operation/snapshot → live absence preflight → create quota → adapter write → task reconciliation. Update path дополнительно требует linked listing exact identity, reconstructable current full payload и update quota.
- Create создаёт только отсутствующий `offer_id`. Найденный upstream offer, включая archived, завершает operation ошибкой до quota/write; update существует только как отдельные `product_update|product_update_rollback` kinds и никогда не маскируется create path.
- `submitting` и `attempt_count > 0` выставляются и commit-ятся до вызова write adapter. Transport/5xx/malformed success после этого считается ambiguous; повтор `/v3/product/import` запрещён. Definitive validated API rejection может стать `failed`.
- Poll/status response обязан совпасть exact-set по offer, иметь bounded items/errors и известные statuses. Неполный, foreign, duplicate или malformed response не считается успехом. После 24 часов неудачного task polling automatic retry прекращается с видимым `uncertain`; ручной poll остаётся возможен.
- Update task status `imported` сам по себе не является success: info, attributes, base price и current pictures обязаны снова сложиться в exact submitted fingerprint. Prior-state означает bounded ожидание visibility, третье состояние — external drift и terminal-visible `uncertain` без нового write.
- Live reconciliation без task id для create разрешена только когда committed before-state доказывает отсутствие offer до write. Для update она сравнивает exact prior/submitted/third state. `uncertain` сохраняет credentials; audited manual stop освобождает только local quota и оставляет outcome неизвестным.
- Выключение `MARKETPLACE_OZON_PUBLICATION_ENABLED` запрещает новый write и отправку безопасной queued operation, но не отменяет уже начатую сверку. Disconnect не может удалить ключ, нужный для reconciliation.
- Create compensation архивирует exact product только при unchanged full-state. Update compensation создаёт второй explicit operation и восстанавливает prior full payload только при submitted-state drift gate. Media — replace-style часть полного payload: `primary_image + images <= 30`, optional `color_image`, `images360` запрещён; picture read errors или непредставимый старый state блокируют write.
- Миграция `migrate_add_marketplace_product_updates.py` идемпотентно расширяет CHECK contracts после commercial migration, сохраняет operation/snapshot/proposal FK rows и подключена fail-fast в Docker entrypoint.

### Marketplace-scoped auto-publish

- `AutoPublishSettings` уникален по Ozon `account_id`; только WB имеет partial unique seller row с `account_id=NULL`. `AutoPublishRun.settings_id` обязателен, а run/item дублируют marketplace/account scope для fail-closed query и аудита. Не возвращайте seller-only queries в scheduler/routes/retry/restart recovery.
- Draft provisioner принимает до 200 уникальных positive integer ImportedProduct IDs, до первого create проверяет exact seller set и создаёт одну локальную проекцию на каждый enabled active Ozon account. Он не вызывает adapter/LLM. Один failed draft не откатывает уже завершённый supplier import; ошибка остаётся bounded и повторно подхватывается очередью.
- Ozon queue валидирует strict settings, source fact hash и deterministic draft schema до provider boundary. Advisory account quota вычитает local active reservations; фактическая publication повторно делает operation-level live quota check. Daily/provider хвост помечается `deferred`, не `completed`.
- Item idempotency key и `submitting` claim commit-ятся атомарно только если exact run всё ещё `running|waiting`, settings enabled и не paused. Provider operation commit-ится до HTTP. При restart operation ищется по exact seller/account/draft/kind/key; отсутствие key означает доказанное prewrite состояние и безопасный defer, а не blind retry.
- `cancelling` входит в reconciliation, но запрещает новые submit claims. Уже созданная/claimed operation продолжает read-only reflection; terminal `uncertain` переводит run в `attention`. Отдельные bounded scheduler jobs reconciles durable marketplace operations и отражают их в auto-publish items; выключенные flags не бросают attempted writes.
- Retry/cooldown/exhaustion вычисляются только по последней account-scoped попытке товара, чтобы старая failure row не блокировала явный retry навсегда. Circuit breaker, lock, counter и notifications принадлежат одному settings/account scope и не влияют на WB или другой Ozon cabinet.

### Ozon commercial price/stock operations

- Proposal creation — read-only граница: она читает live price либо exact `free_stock` одного owned FBS/rFBS warehouse, сохраняет exact before/proposed fingerprints и остаётся `pending_review`. Aggregate stock summary и FBO stock не являются write source.
- JSON route не coercion-ит boolean/float/numeric string в IDs/stock; price принимает decimal string либо integer, но не float. Public serializers не возвращают credentials, raw provider response или idempotency key.
- Approve требует отдельный commercial write flag, seller-owned reviewer, явный `confirm_write=true` и exact optimistic version. Под account lock он повторно читает live state; несовпадение с proposal baseline даёт `conflict` без operation/write.
- До HTTP создаются operation+snapshot, затем отдельным commit фиксируются `submitting` и `attempt_count=1`. После начала вызова автоматический повтор price/stock write запрещён; definitive failure, malformed response и ambiguous transport различаются, но неизвестный результат всегда подтверждается только live read.
- Batch approve принимает exact-set 1..100 уникальных proposal IDs одного account и одного kind. Preflight и read-after-write используют общий paginated provider read, затем выполняется ровно один exact-set provider write; каждый item всё равно имеет отдельные operation, snapshot, result, reconciliation и rollback. Не заменяйте bulk read/write циклом API-вызовов на карточку.
- `succeeded` требует exact live fingerprint proposed state. Live before-state означает bounded polling без retry write; третье состояние означает `conflict/uncertain` и запрещает blind rollback.
- Rollback исходного succeeded price/stock update создаёт второй `pending_review` proposal только если live state всё ещё exact original submitted state. Его approve восстанавливает exact original before-state и проходит тот же single-attempt/reconciliation путь.
- `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=0` запрещает approve и queued submission, но minute scheduler продолжает reconciliation уже attempted operations. Credential mutation/disconnect не могут удалить ключ, нужный для attempted/applying/uncertain операции.

### Ozon quality и analytics

- `/v1/analytics/data` — read-only POST в endpoint manifest с capability `analytics_read`. Request имеет только точные `date_from/date_to`, один dimension (`sku` либо `day`), фиксированный список метрик, пустые filters/sort и bounded `limit/offset`; credentials не входят в payload.
- `MarketplaceAnalyticsSync` является durable попыткой exact account/period. Product и day pages commit-ятся по одной; duplicate dimension между страницами, drift totals, malformed/foreign/NaN/negative значения или превышение 20 000 строк fail-closed завершают только текущую попытку. UI читает последний полностью `completed` snapshot и никогда не смешивает partial facts failed run.
- `MarketplaceMetricFact` хранит normalized code, исходный provider metric, unit, definition code, endpoint и `cross_marketplace_comparable=false`. Ozon revenue/orders/views/conversion нельзя суммировать или ранжировать вместе с WB без отдельного явно версионированного normalization contract.
- Ozon SKU сопоставляется с listing только внутри exact seller/account по `primary_sku`, `sku|sku_fbo|sku_fbs` и bounded sources. Один SKU у двух локальных listings блокирует provider read; неизвестный SKU сохраняется как unmatched fact без fake listing.
- `MarketplaceQualityAssessment` — отдельная проекция для `entity_kind=marketplace_listing`; WB quality поля в `Product` не перезаписываются. Scorer не вызывает LLM/provider и использует provider-specific reason codes вместе с общей severity/impact.
- Quality score существует только при fresh Ozon tree/type schema, полном locally consistent attribute definition set и listing attributes snapshot не старше 48 часов. Missing/stale truth даёт `score=NULL` и `schema_stale|unscorable`, а не guessed score. Required attributes имеют больший вес; Ozon `is_aspect` нормализуется в `is_filterable` и оценивается отдельно.
- Performance-причины quality используют только свежий завершённый current-period 30d snapshot того же account. Старый/failed/partial snapshot означает `ozon_no_analytics_signal`; он не выдаётся за нули и не влияет на карточку другого кабинета.
- Scheduler каждые 10 минут обрабатывает максимум три connected Ozon accounts и максимум две analytics pages на account, затем bounded пересчитывает quality. Этот job только читает Ozon; feature flag выключает новые вызовы. File claim достаточен только для текущего singleton-host Compose, как и остальные account locks.

### Ozon заказы, возвраты и отмены

- Не записывайте Ozon fulfillment в `WBOrder`, `WBSale`, `WBRealizationRow` или `FinanceSnapshot`. `MarketplaceFulfillmentSync`, `MarketplacePosting`, `MarketplacePostingItem`, `MarketplacePostingStatusEvent`, `MarketplaceReturn` и `MarketplaceCancellation` являются отдельными exact-account projections; fake `nm_id` запрещён.
- Current endpoint boundary: `/v4/posting/fbs/list`, `/v3/posting/fbo/list`, `/v1/returns/list`, `/v2/returns/rfbs/list`, `/v2/conditional-cancellation/list`. Заменённые postings FBS v3/FBO v2 и conditional cancellation v1 нельзя добавлять даже как fallback. Устаревающие finance transaction v3 также не являются fallback для P9B.
- `ozon_fulfillment_contracts` строит периоды не длиннее 31 дня, явно отключает posting analytics/financial data/barcodes и принимает только bounded offset/cursor pages. Duplicate posting/product/event identity, non-advancing cursor, malformed timestamp/decimal/boolean или over-limit response отклоняют страницу до ORM write.
- Buyer name, phone, address, email, client comments/photos, return-place address, barcodes и произвольный raw provider body не сохраняются. Разрешённый whitelist: posting/order identities, explicit fulfillment source, provider status/substatus/reason enums/timestamps и product offer/SKU/name/quantity/price/currency. Posting price не является finance truth и не используется для прибыли.
- Пять фаз fulfillment sync commit-ятся по одной полностью проверенной странице и могут resume по durable offset/cursor. Partial/failed run не удаляет существующие projections и не помечает unseen rows отсутствующими. Status history добавляется только при реальном изменении status/substatus; отмена создаётся только из явного provider cancellation status/timestamp/reason или current conditional-cancellation feed.
- Offer/SKU сопоставляется только внутри exact seller/account. Неоднозначный локальный SKU блокирует provider read; неизвестный SKU остаётся `listing_id=NULL` и наблюдаемым unmatched counter. Route selector scope задаётся query, body scope smuggling запрещён.
- Scheduler каждые 10 минут выбирает максимум два connected Ozon accounts, обрабатывает максимум пять страниц каждого и только читает provider. UI может выполнить два таких bounded шага и честно показывает незавершённую фазу; следующий scheduler/manual run продолжает её.

### Ozon финансы

- Не записывайте Ozon finance в `FinanceSnapshot`, `WBRealizationRow`, posting `financial_data` или WB P&L. Отдельные `MarketplaceFinanceSync`, `MarketplaceFinanceAccrualType`, `MarketplaceFinanceFact`, `MarketplaceFinanceFactItem` и `MarketplaceFinanceComponent` всегда имеют exact `seller_id + marketplace_id + account_id` scope.
- Current read endpoints: `/v1/finance/accrual/types`, `/v1/finance/accrual/by-day`, `/v1/finance/accrual/postings`. Верхнее `accruals[].accrual_id` обязательно: переименованное 09.06.2026 top-level `type_id` отклоняется. Nested fee `type_id` остаётся type dictionary identity. `/v1/finance/compensation` и `/v1/finance/decompensation` создают report jobs и не являются retryable read feeds/scheduler sources.
- `ozon_finance_contracts` принимает один exact day и bounded opaque `last_id`, signed finite Decimal и currency; duplicate accrual/component/SKU, day drift, non-advancing cursor, malformed money или непустой неизвестный `container_fees` отклоняют страницу до ORM write. Raw provider body, buyer data и overlapping commission snapshots не сохраняются.
- Seller-visible ledger строится только из `accruals[].total_amount`. Positive/negative показываются отдельно; `net = sum(total_amount)` только внутри одной currency. Это не profit. Nested fee имеет `rollup_role=explanatory_only`, cross-currency и WB/Ozon rollup запрещены.
- Sync сначала атомарно обновляет type dictionary, затем commit-ит по одной нормализованной day/cursor page в immutable snapshot. Partial/failed snapshot скрыт; UI продолжает отдавать последний covering completed snapshot. Completed history bounded до восьми snapshots на account/period. SKU и posting связываются только exact-account; ambiguous SKU остаётся несвязанным и явно считается.
- `/marketplaces/finance` и API требуют canonical positive query `account_id`; body scope smuggling запрещён. Scheduler каждые 10 минут выбирает максимум два connected Ozon accounts и читает максимум пять страниц каждого; feature flag выключает новые calls, write flags ему не нужны.

### Ozon отзывы и вопросы

- Current read boundary: `/v2/review/list` с фазами `NEW|VIEWED|PROCESSED` и `/v1/question/list` с теми же durable status-фазами. Старый review list v1 и произвольный endpoint fallback запрещены. Эти методы могут требовать Premium Plus: account получает `reviews_read|questions_read` только когда `/v1/roles` содержит соответствующий exact path; отсутствие capability не считается поломкой всего кабинета.
- `ozon_feedback_contracts` строит окно ровно 90 дней, limit не больше 100, canonical UTC date range и opaque advancing cursor. Response обязан быть bounded, с уникальными ID, точным requested status, timezone-aware timestamp и review rating 1..5; status/date escape, duplicate identity, changed SKU или повтор между страницами/фазами fail-closed отклоняют текущий run.
- `MarketplaceInboxSync` commit-ит каждую полностью проверенную страницу и resume-ится по `status + last_id`. `MarketplaceInboxItem` связывает SKU только внутри exact seller/account; ambiguous/unmatched не получает fake listing. Customer author/name/links/product URL/raw response не сохраняются, customer text хранится максимум в 90-дневном окне. Изменение source fingerprint supersede-ит активный draft.
- `/marketplaces/reviews` и API принимают account scope только canonical query-параметром. UI разделяет WB и Ozon, отзывы и вопросы, показывает capability/Premium state и явно сообщает, что ничего не отправлено. Scheduler раз в 15 минут выбирает максимум две capability-proven `account + source_kind` пары и читает максимум три страницы каждой; feature flag выключает новые provider calls, но bounded local retention cleanup продолжает удалять просроченный customer text.
- P10A не регистрирует review/question write endpoints, adapter methods, capability writes или кнопку отправки. AI/template создают только `MarketplaceReplyDraft(status=draft)`; provider send всегда `false`. Пустой отзыв без text/photo/video не получает draft. Один AI draft использует seller profile, `AIConfig.max_retries=1`, bounded customer text/facts и output cap 500 tokens; `log_payloads=false` запрещает prompt/response/provider-body debug logs. UI явно сообщает о передаче этих данных настроенному AI-провайдеру, customer text и listing facts маркируются как data/untrusted instructions, а HTML/link/email/phone/discount/compensation output отклоняется. Перед commit source/facts fingerprints перечитываются; concurrent drift отклоняет результат, а partial unique active-draft index превращается в явный conflict. Template mode полностью локален. Любая будущая отправка требует отдельного audited proposal/confirmation/idempotency/reconciliation этапа.

## LLM policy, budgets и prompt cache

- Новая seller AI-настройка создаётся с `provider=deepseek`, primary model `deepseek-v4-pro` и `agent_single_model=false`. Orchestration task types `plan_request`, `smart`, `custom`, `pipeline` используют seller primary model, обычно DeepSeek Pro.
- Internal execution skills используют DeepSeek Flash с `thinking.type=disabled`, если `agent_single_model` не включён. Pro-планирование сохраняет provider-default thinking. Никогда не переносите key/base URL между providers при fallback/model switch.
- Seller-scoped AI profile имеет приоритет. Credentials передаются task-scoped через authenticated internal API и не записываются в логи.
- Default budgets определены в `agents/config.py`: `AGENT_RUN_TOKEN_BUDGET=30000`, `AGENT_RUN_API_BUDGET=24`, `AGENT_MAX_PRODUCTS_PER_RUN=200`, `AGENT_OBSERVATION_MAX_CHARS=1200`. Изменение defaults требует тестов и обновления этого файла.
- При исчерпании budget возвращайте честный partial result без дополнительного LLM call. `llm_retry` считает каждую физическую попытку в `usage.api_requests`, а execution-path ограничивает retries фактическим остатком API-бюджета; параллельные чанки заранее делят общий лимит и не могут превысить его суммарно. Сохраняйте cancellation checks и durable skill-boundary checkpoints.
- Для больших наборов используйте prefetch, bounded chunks, batch endpoints и bounded concurrency. Не создавайте N+1 DB/API/LLM calls.
- Если точный parser уверенно извлёк явно названные поля контента, его `title|description` mask является верхней границей и semantic planner не может её расширить. При miss или опечатке planner может выбрать только значения из того же закрытого enum; свободное имя поля не принимается. `content-writer` принимает максимум 100 typed positive integer IDs без coercion из boolean/float/string, делает один content-brief query, затем Flash chunks: до 24 карточек для title-only и до 8 для description/both, дополнительно ограничивая prompt примерно 12 000 символов. Каждый чанк обязан вернуть точное множество уникальных integer IDs и все поля; stop-word response обязан полностью совпасть по `(product_id, field)`. Любой пропуск, дубль, чужой ID или неполная проверка блокирует запись чанка без LLM retry/ReAct fallback. Product/ImportedProduct сохраняются batch endpoint-ами с optimistic `expected_updated_at`, snapshots/history и честными changed/unchanged/failed counts. Если фоновая синхронизация изменила только `updated_at`, runtime один раз перечитывает brief и повторяет тот же уже проверенный diff с новым optimistic timestamp без второго LLM-вызова; реальное изменение title/description блокирует перезапись как conflict.
- Cancellation проверяется до prefetch, до и после каждого LLM chunk, перед postprocess/tool/write и перед commit. После отмены не планируйте новые futures и не исполняйте уже сгенерированные tool calls. Structured batch error не должен автоматически переключаться на дорогой ReAct; usage сохраняется в partial/failed result и checkpoint.
- Не запускайте LLM-классификатор перед точным запросом. Regex/enum/typed SQL остаются только узким полнофразным fast-path; после любого deterministic miss, опечатки или составной цели непустой запрос маршрутизируется одним `plan_request` на seller primary model (обычно Pro) с компактным стабильным capability catalog, максимум шестью шагами и output cap 2200 токенов. Planner получает bounded durable state последнего plan/run/clarification, поэтому продолжение понимает фактический результат предыдущего шага, а не только текст пользователя. Python повторно валидирует typed scope и `scope_mode`, skill allowlist, параметры и risk, игнорирует model-reported risk и не разрешает semantic plan расширить запрет на writes. Semantic write без выбранных IDs допустим только после typed supplier selection либо при явной фразе о всём каталоге; старая выборка не превращается в global write по догадке модели. Для semantic `catalog-query` не выполняется отдельный Flash polish: planner + typed SQL остаются одним LLM-вызовом. Usage planner переносится в execution run и учитывается в общем API/token budget.
- Task mutation endpoints (`start`, `progress`, `checkpoint`, `complete`, `fail`) возвращают только компактный статус. Полные `input_data`, `checkpoint` и `result` доступны лишь в poll/get flows и не должны эхом передаваться worker на каждом обновлении.
- DeepSeek prompt cache автоматический. Стабильные system instructions, JSON schema и tool definitions должны идти до динамического user/task content. Не добавляйте timestamps, IDs или перестановку schema/tools в стабильный prefix.
- Сохраняйте usage: input/output, API requests, cache hit/miss tokens, reasoning tokens, requested model breakdown и cost where available. Cached tokens входят в input и не должны второй раз добавляться в total. UI должен отличать `Без LLM`, Flash execution и Pro orchestration по фактическим запросам, а не по загруженному primary profile.
- `cost_usd` означает provider-reported cost; `estimated_cost_usd` хранится отдельно и требует актуальной документированной pricing table.

### Retrieval policy

- Structured seller truth (`Product`, `ImportedProduct`, defaults, categories/characteristics, pricing, stock, stop-words, API logs and live statuses) читается только typed SQL/tools и не индексируется как RAG corpus.
- Неструктурированные правила WB и проверенные инструкции загружаются только явно через `scripts/manage_agent_knowledge.py`. Разрешены source types `wb_official|seller_policy|platform_guide|official_reference`; документ неизменяем в пределах `scope_key + source_key + version`, хранит SHA-256 checksum, а новая версия атомарно архивирует предыдущую. Для `wb_official|official_reference` обязателен будущий `valid_until`; просроченный документ fail-closed исключается из выдачи. Не индексируйте весь `docs/`, код, `AGENTS.md`, логи, секреты и устаревшие guides автоматически.
- Retrieval находится в `services/agent_knowledge.py`: SQLite FTS5 prefix retrieval объединяется с Unicode casefold prefix fallback и trigram rerank; видимость всегда `seller_id IS NULL OR seller_id = task.seller_id`. Internal endpoint `/internal/v1/sellers/<seller_id>/knowledge/search` требует agent auth, активный assigned task и совпадение seller scope.
- Retrieval возвращает не более 8 фрагментов и 6 000 символов вместе с `citation_id`, title, version, source URI и heading. Явная фраза «по базе знаний/правилам WB» маршрутизируется deterministic-first без LLM-классификатора; semantic miss может выбрать тот же read-only skill. Синтез ответа делает один Flash-вызов без thinking с cap 700 output tokens; Python отклоняет неизвестные citation IDs. При empty retrieval, ошибке synthesis или нехватке token budget возвращается bounded deterministic result без дополнительного LLM call. Pro не используется для retrieval или synthesis.
- Качество проверяется JSON-наборами `query + expected_source_key` командой `manage_agent_knowledge.py evaluate`; она считает Recall@K и MRR. Новые corpus/ранжирующие изменения должны добавлять реальные misses в evaluation dataset и тестировать tenant isolation, version archive, strict context cap и citations.
- Embeddings/vector DB, RAPTOR и GraphRAG добавляются только после измеренных retrieval misses или появления достаточно большого связного корпуса. Не вводите их как замену SQL или без evaluation dataset.

### Runtime caveats

- Для SQLite запускайте один активный `agent-orchestrator`. Poll и `start_task()` не являются полноценным atomic queue claim на SQLite; несколько replicas могут взять одну задачу.
- Cancellation проверяется между шагами. Side effect должен быть коротким, идемпотентным и повторно проверять ownership/state перед commit.
- Импорт `seller_platform.py` запускает APScheduler, если не установлен `SKIP_SCHEDULER=1`. Тесты, миграции и one-off scripts должны отключать scheduler.
- TLS verification можно ослаблять только внутри контролируемой локальной/Docker-сети. Не переносите insecure defaults на внешний agent endpoint.
- Docker healthcheck обязан задавать внутренний network timeout короче container timeout и закрывать ответ. Agent liveness обновляется локальным потоком без I/O и не зависит от доступности platform heartbeat/poll.
- Gunicorn запускает несколько web workers, поэтому APScheduler выбирает один процесс через advisory file lock `SCHEDULER_LOCK_FILE` (по умолчанию `/tmp/seller-platform-scheduler.lock`). Workers без lock проверяют возможность takeover каждые `SCHEDULER_LOCK_RETRY_SECONDS` (по умолчанию 15 секунд), чтобы graceful reload не оставил процесс без scheduler. Не удаляйте этот lock и не запускайте второй scheduler в том же контейнере; для нескольких web containers нужен отдельный singleton scheduler runtime.

## UI и темы

Актуальная визуальная система называется «Тёплая редакция». `templates/base.html` является единственным источником palette tokens.

- Поддерживайте обе темы через `data-theme="light|dark"` и сохранённый ключ `sh-theme`.
- Используйте CSS variables `--bg`, `--bg-card`, `--bg-hover`, `--text`, `--text-secondary`, `--text-muted`, `--accent`, `--accent-strong`, `--accent-light`, `--border` и semantic status tokens. Не добавляйте отдельную несвязанную palette.
- Используйте `Inter` для рабочего интерфейса; `Instrument Serif` оставляйте для редких display-акцентов.
- Новый UI должен работать в Jinja2 + Alpine.js + существующем Tailwind CDN setup. Не вводите bundler без архитектурного решения.
- Для agent chat меняйте `static/agent-chat.css`, `static/agent-chat.js`, popup assets и templates согласованно.
- Большие результаты показывайте одной сворачиваемой collection-card: список не разворачивается автоматически, карточки можно выбрать и передать в следующий audit/write-plan. Не рендерите десятки отдельных artifact cards и не дублируйте их текстом в ответе.
- Выбор из collection-card обязан сохранять `entity_kind` вместе с IDs до `entity_scope` задачи. Числовой ID без типа нельзя передавать из WB `Product` collection в legacy `ImportedProduct` skills.
- Chat polling активен только для `queued/running` запуска. Терминальный или пустой диалог не должен создавать постоянные GET/SQLite write cycles; при возврате вкладки выполняется одно обновление.
- Интерфейс operational: компактный, сканируемый, без marketing hero, gradient/orb decoration и вложенных cards. Радиусы основных cards не более 8px.
- Используйте существующие `.sh-*` components и tokens. Новые controls должны иметь keyboard/focus states, labels/ARIA и не перекрывать контент на mobile/desktop.
- Mutating fetch requests должны передавать CSRF token из meta/header по существующему паттерну.
- После заметного UI-изменения проверьте light/dark, desktop/mobile, empty/loading/error/disabled states и отсутствие horizontal overflow.

### Общий UI-слой и инварианты

- Токены-масштабы в `base.html`: радиусы `--r-xs/sm/md/pill` (карточки ≤ `--r-md` = 8px),
  elevation `--shadow-1/2/3` (только оверлеи/hover, не плоские карточки), мотн
  `--ease-out/-in/-in-out` + `--dur-1/2/3`, единая z-шкала оверлеев
  `--z-dropdown/-sticky/-backdrop/-overlay/-toast/-cmdpal`. Chart-серии должны идти
  через отдельную категорийную палитру (не через семантические status-токены);
  токенизация графиков — открытый follow-up.
- Все интерактивные `.sh-*` обязаны иметь `:focus-visible` (кольцо `--focus-ring`) и
  `:active`. В `base.html` есть глобальный `@media (prefers-reduced-motion: reduce)` —
  не добавляйте немаскируемую анимацию. Статусные компоненты (`.sh-alert`,
  `.sh-confirm-icon`, `.sh-btn--primary/--danger`, пагинация, dropdown-danger) идут
  через семантические токены и обязаны работать в обеих темах; не хардкодьте hex.
- Новые примитивы в `static/sh-ui.css`: `.sh-skeleton`, `.sh-spinner`, `.sh-progress`,
  `.sh-toggle`, `.sh-segmented`, `.sh-chip`, `.sh-avatar`, `.sh-stepper`, `.sh-icon-btn`,
  `.sh-btn.is-loading`. Есть Jinja-макросы в `macros/components.html`
  (`skeleton/spinner/progress/toggle/segmented/chip/avatar/stepper`). Инлайн-`#hex`
  статусов в `style="..."`/`[#hex]` запрещён — используйте `var(--danger/-ok/-warn/-info[-bg/-border])`.
- Иконки — единый реестр `templates/macros/icons.html`: `icon(name, size, stroke, cls)`
  и `status_icon(type)`. Не плодите инлайн-`<svg>` в общих поверхностях и не используйте
  emoji/unicode как иконки контролов. `btn/stat_card/empty_state/alert_box` принимают имя
  иконки из реестра ИЛИ готовую `<svg>`-строку (обратная совместимость).
- Уведомления — единая система: один Alpine-стор `$store.toasts` (тосты) и `$store.notif`
  (unread/поллинг/центр/mute/относительное время) в `static/sh-ui.js`. НЕ создавайте
  второй стор или контейнер тостов. Тосты рендерит `toast_container()` (theme-aware
  `.sh-toast` с категорийным рейлом, action-ссылкой, progress, pause-on-hover); центр —
  `partials/notification_center.html` (колокольчик+поповер). `toast_store_init()` —
  no-op (устарел). Звук: `error`→нисходящая мелодия, mute в `sh-notif-muted`. Даты с
  сервера naive-UTC — при парсе в JS добавляйте `Z`.
- Оболочка: слим-топбар в `base.html` (видимый «Поиск ⌘K` → событие
  `toggle-cmdpalette`, колокольчик-поповер, one-click тема, слот `{% block topbar_left %}`
  для крошек `.sh-crumbs`). Сворачивание сайдбара персистится в `sh-sidebar`. Command
  palette реально фильтрует (`shCmdPalette()`), навигация ↑↓/Enter.
- Оверлеи: подключён `@alpinejs/focus`; modal/confirm/slideover/bottomsheet/cmdpal имеют
  `x-trap.noscroll.inert` (focus-trap + scroll-lock + inert + возврат фокуса). Панели
  оверлеев обязаны быть выше общего `.sh-backdrop` (`z-index:1`). Радиусы/бэкдропы/easing
  сведены к токенам — не вводите разнобой.
- Новые общие ассеты подключены в `base.html`: `static/sh-ui.css` и `static/sh-ui.js`
  (последний — ДО Alpine core, чтобы `x-data`-фабрики и сторы были готовы вовремя).

## Coding conventions

- Сначала прочитайте соседние route/service/model/tests и следуйте локальному паттерну.
- Сохраняйте UTF-8. Имена кода преимущественно английские; пользовательский текст и доменные комментарии могут быть русскими.
- Держите routes тонкими: auth, parse, service call, response. Транзакции и доменные правила должны быть тестируемыми.
- Используйте SQLAlchemy queries и structured parsers. Не собирайте SQL/JSON/URLs небезопасной конкатенацией.
- На exception после изменения session вызывайте `db.session.rollback()` или откатывайте savepoint.
- Внешние HTTP calls должны иметь timeout, ограниченный retry/backoff, rate-limit handling и sanitized errors.
- Не делайте unrelated refactor, массовое форматирование и churn в большом `seller_platform.py`/`models.py` без необходимости.
- Не меняйте и не удаляйте пользовательские или параллельные worktree changes. Не используйте destructive git commands.
- Никогда не коммитьте `.env`, database files, credentials, screenshots с чувствительными данными или generated debug dumps.

## Definition of done

Перед завершением задачи проверьте:

1. Поведение реализовано end-to-end, а не только на одном UI/API уровне.
2. Tenant scope, proposal-only protected fields, snapshots/rollback и tool allowlists сохранены.
3. Добавлены узкие тесты и выполнены доступные проверки; непройденные проверки явно указаны.
4. Нет секретов, реальных external calls и destructive migration side effects.
5. UI проверен в обеих темах и релевантных responsive states.
6. Если изменились архитектура, логика, команды или перечисленные policy/invariants, `AGENTS.md` обновлён в том же изменении.
