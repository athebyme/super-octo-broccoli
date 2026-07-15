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
- `services/marketplace_fact_pack.py`: marketplace-neutral observed facts с field-level provenance; legacy AI output остаётся отдельным unverified suggestion.
- `services/marketplace_drafts.py`, `routes/marketplace_drafts.py`: seller/account-scoped Ozon drafts, exact category mappings, optimistic edits и deterministic publishability validation без provider/LLM calls.
- `services/ozon_product_import.py`: whitelist-only контракт `/v3/product/import`, строгая нормализация task status и quota response.
- `services/marketplace_publications.py`, `routes/marketplace_operations.py`: durable seller-scoped Ozon create operations, snapshots, manual submit, polling/reconciliation и audit UI/API.
- `services/marketplace_operation_locks.py`: account-level non-blocking file lock для publication, credential mutation, health check и disconnect в одном host/filesystem namespace.
- `scripts/probe_ozon_read_contracts.py`: optional live read-only contract probe. Он принимает credentials только из process env или owner-only `/tmp/ozon_live.env`, вызывает исключительно manifest endpoints с `retry_class=read` и выводит только bounded response shapes без scalar values/offer/SKU/raw errors.
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
- Актуальный Ozon manifest использует description-category v1, product list/info v3, product attributes v4, pictures read v2, product import v3 + status v1, limits v4, prices read v5/update v1, aggregate stocks read v4, per-warehouse FBS read v2, per-warehouse FBO read v1, stocks update v2, warehouses v2 и finance feeds v1. Deprecated category/product endpoints, per-warehouse FBS v1, warehouse v1 и отключённые finance v3 запрещены. В `/v3/product/import` `offer_id` обязателен, а `images360` удалён 10.07.2026.
- `services/ozon_commercial_contracts.py` является ORM-free fail-closed boundary для P6: price/stock builders принимают только whitelist-поля и exact identities, response обязан быть exact-set без чужих/повторных/пропущенных результатов, stock всегда содержит точный `warehouse_id`, а warehouse/FBS pages требуют корректную cursor pagination. Platform batch cap равен 100 даже там, где upstream допускает больше. Контракт ещё не даёт права на provider write: такой write допустим только после durable snapshot, human approval и drift preflight.
- `scripts/probe_ozon_read_contracts.py` не имеет write mode, загружает live credentials только из process env или owner-only файла и выводит только bounded shapes. Из `/v1/roles` он сохраняет только фиксированные boolean-проверки известных методов, не role names и не произвольные method values. Не расширяйте probe endpoint-ом, который не помечен `retry_class=read`.
- Seller account/catalog/draft UI и live Ozon checks включаются через `MARKETPLACE_OZON_ENABLED=1`; новый product write требует также `MARKETPLACE_OZON_PUBLICATION_ENABLED=1`. Оба default `0`. Выключение publication flag запрещает отправку queued rows, но scheduler продолжает submitted/polling/uncertain reconciliation.
- Ozon reference truth хранится отдельно от WB-shaped `MarketplaceCategory`: `MarketplaceTaxonomyCategory`, `MarketplaceProductType`, `MarketplaceAttributeDefinition` и `MarketplaceAttributeValue`. Идентичность типа всегда `description_category_id + type_id`; value ID никогда не переносится между attribute/type scopes.
- Global Ozon taxonomy использует только явно настроенный `MarketplaceReferenceAccount`, никогда случайный seller key. Tree обновляется каждые 24 часа, stale enabled schemas bounded-пакетом каждые 6 часов; required dictionaries синхронизируются eager в общем dictionary budget. Все scope jobs используют non-blocking file claims.
- Последний полный Ozon snapshot остаётся structured truth до hard TTL 48 часов. Empty/malformed/duplicate/partial/anomalously shrunk ответ не меняет reference rows. Dictionary checkpoint наблюдаемый и не является resume cursor: без staging retry обязан начать с нуля. Admin restriction может быть только exact subset fresh official dictionary; required attribute нельзя отключить.
- `MarketplaceListing` является общей published read projection, но не master product: Ozon row всегда содержит seller/account/marketplace scope и раздельные opaque `offer_id`, `external_product_id` и SKU; WB row является временным backfill с `legacy_product_id` и nullable account. Ozon никогда не получает fake `Product.nm_id`.
- `MarketplaceCatalogSync` хранит durable phase/cursor/total/counters. Ozon sweep идёт по `ALL`, затем `ARCHIVED`; страница list + product info + attributes + prices + stocks полностью валидируется до одного commit. Pause/failure сохраняет cursor и последний read model, но не снимает availability. Missing rows помечаются только finalizer-ом после полного прохода обеих фаз; force restart начинает новый run с первой страницы.
- Seller catalog UI/API находится на `/marketplaces/listings/`, фильтруется только внутри `current_user.seller`, а внешний read запускается только для составного `seller_id + account_id` после connected/active/expiry checks. Один account сериализуется DB running-index и non-blocking file claim; один HTTP запуск ограничен 50 list pages, UI использует bounded 5-page пакет.
- `MarketplaceProductDraft` является отдельной seller/account-specific проекцией `ImportedProduct`, а не HTTP body и не `Product`. `MarketplaceCategoryMapping` уникален внутри seller + marketplace + supplier/source scope + normalized source category; автоматически применяется только `active` exact mapping на доступный enabled type.
- Draft UI/API `/marketplaces/drafts/` использует optimistic `expected_version`; draft/account/imported source проверяются составно с seller. Выключенный feature flag блокирует draft writes, но оставляет существующее состояние read-only.
- Ручная Ozon publication P5a является create-only: existing offer в `ALL|ARCHIVED` блокирует write и требует будущего отдельного update workflow. Перед HTTP write сервис повторно валидирует весь draft/schema, строит whitelist payload, фиксирует operation+snapshot, проверяет live offer и `/v4/product/info/limit`, резервирует quota и только затем вызывает adapter.
- `MarketplaceOperation` хранит durable lifecycle и bounded sanitized results, а `MarketplaceListingSnapshot` — exact submitted/confirmed state. Raw provider response, idempotency key, credentials и submitted payload не возвращаются public routes. Один account write/reconcile сериализуется file claim; scheduler обрабатывает bounded due batch каждую минуту.
- Автоматический rollback созданной карточки намеренно не включён: на дату аудита 15.07.2026 точный актуальный официальный archive-контракт не подтверждён. Snapshot фиксирует `automatic_rollback_contract_unverified` и manual archive instruction; не подменяйте archive beta-методом visibility и не добавляйте guessed endpoint.
- Изменение Client-Id/API key, connection recheck и disconnect используют тот же account lock. Credential mutation блокируется при любой active operation; disconnect отменяет только `queued` с `attempt_count=0`, но сохраняет ключ для submitted/polling/uncertain и любого write с ненулевой попыткой.
- File claim координирует процессы только на общем host/filesystem. Текущий Compose имеет singleton scheduler и один web container; перед multi-host/web-replica rollout P11 обязан заменить claim распределённой блокировкой либо гарантировать shared lock filesystem.

### Единый AI-помощник

- `routes/agents.py`: seller-scoped chat, run, cancel, rollback и proposal review endpoints.
- `services/agent_harness.py`: conversations/messages, plan confirmation, task tree, checkpoints, proposals и rollback orchestration.
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

Основной поток: browser chat -> `routes/agents.py` -> `agent_harness` -> `AgentTask` -> poll единого orchestrator -> `UnifiedSellerAgent` -> internal skill -> tools -> authenticated internal API -> DB -> conversation polling -> UI. Точные безопасные intents планируются deterministic-first. Любой непустой запрос, который не разобран этим первым уровнем (включая короткую разговорную формулировку), получает один bounded structured semantic plan или конкретный clarification; длина текста больше не является условием вызова planner. Planner видит только текущий запрос, typed scope без списка ID, нормализованный page context и до 6 предыдущих языковых реплик общим объёмом до 2400 символов. Read-only semantic plan стартует автоматически, write-plan требует подтверждения. Явные вопросы к инструкциям идут в `knowledge-query`: task-scoped internal retrieval выбирает только global + документы текущего seller, после чего bounded Flash синтезирует ответ с проверенными citations; при отсутствии результата или бюджета возвращаются детерминированные cited excerpts без догадок.

Частые read-intents (цены, остатки, пропуски контента, import/publication status, supplier publication counts, WB catalog counts, API health, defaults, stop-words и pricing settings) обязаны сначала проходить строгий локальный parser и typed SQL/internal endpoint. Не используйте LLM как классификатор там, где intent и параметры можно проверить regex/enum. Generic count/list fast-path принимает только целую фразу без неизвестных модификаторов: «покажи просевшие карточки» нельзя молча превращать в выдачу всего каталога. Для deterministic catalog query допускается один короткий Flash-вызов только для формулировки ответа; в него передаются condition/count/has_results, но не карточки и не история диалога. Точные supplier counts возвращаются без polish-вызова.

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
`entity_kind='product'` + `product_ids`, лимит 50); write-путь остаётся
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
- Default `reference_guided` boundary: Gen-API/AITunnel получает локально
  подготовленный 3:4 canvas с главным foreground; в `reference_set` остальные
  выбранные байты идут отдельными identity references в порядке сохранённого
  manifest. `packaging`/`detail` являются только evidence и не должны появляться
  лишним объектом. `background_only` остаётся контрольным режимом и единственным
  режимом GPU bridge. Bounded visual context выбирается между ImportedProduct и
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
  original RGB из `reference_guided` и не разрешайте auto-publish по rating.
- Gen-API `image_urls` имеет контракт `files_array`: локальные референсы Flux 2
  отправляются как повторяющиеся multipart-поля `image_urls[]`, не JSON data URI.
  AITunnel `gpt-image-2` поддерживает `reference_guided` через
  `/v1/images/edits`, `image[]`, protection mask и `input_fidelity=high`; для
  вертикального запроса используется поддерживаемый provider size 1024×1536,
  после чего локальный финал нормализуется до 900×1200.
- После provider response `services/infographic_quality.py` повторно локально
  накладывает foreground: alpha берётся из rembg, RGB — строго из декодированного
  оригинала, разрешены только resize/translate. Reference input и финал обязаны
  совпасть по foreground hash и placement; mismatch блокирует job. AITunnel также
  получает protection mask, но она не заменяет локальное восстановление RGB.
  В `collage` каждый foreground имеет отдельный source hash/alpha metadata.
  Финал всегда 900×1200; reference-guided scene остаётся `review_required` до
  human/CV проверки лишних объектов и никогда автоматически не публикуется.
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

Ozon account/catalog/draft UI остаётся выключенным по умолчанию. Для локальной проверки сначала задайте валидный Fernet `ENCRYPTION_KEY`, затем явно экспортируйте `MARKETPLACE_OZON_ENABLED=1`. Seller проверяет кабинет в `/marketplaces/accounts/`, запускает bounded read-only catalog sweep в `/marketplaces/listings/` и готовит локальные validated drafts в `/marketplaces/drafts/`. Draft validation не вызывает Ozon или LLM. Ручной create-write дополнительно и независимо включается `MARKETPLACE_OZON_PUBLICATION_ENABLED=1`; операции и их reconciliation видны в `/marketplaces/operations/`. Никогда не включайте write flag только ради unit tests: они используют synthetic credentials/adapters и не вызывают Ozon.

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
python migrations/migrate_add_image_generation_lab.py data/seller_platform.db
python migrations/migrate_add_image_lab_reference_watermark.py data/seller_platform.db
python migrations/migrate_add_image_lab_angle_synthesis.py data/seller_platform.db
python migrations/migrate_add_marketplace_accounts.py data/seller_platform.db
python migrations/migrate_add_ozon_references.py data/seller_platform.db
python migrations/migrate_add_marketplace_listings.py data/seller_platform.db
python migrations/migrate_add_marketplace_drafts.py data/seller_platform.db
python migrations/migrate_add_marketplace_operations.py data/seller_platform.db
python migrations/run_all_migrations.py data/seller_platform.db
```

Правила schema changes:

- Создавайте новый идемпотентный script в `migrations/`; не полагайтесь только на `db.create_all()` для существующих БД.
- Не переписывайте уже развёрнутую миграцию так, чтобы старые инсталляции получили другое поведение.
- Миграции, добавляющие колонки, которые ORM читает сразу после старта, должны быть fail-fast в `docker-entrypoint.sh`; не скрывайте их ошибку через `|| echo`.
- Подключайте новый script к `docker-entrypoint.sh` и, когда уместно, к comprehensive migration path.
- Держите DDL и backfill повторно запускаемыми; проверяйте наличие table/column/index.
- Backfill обязан явно заполнять Python-side default поля вроде `created_at`: таблица, созданная SQLAlchemy, может не иметь server default даже если новый migration DDL его объявляет.
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
- Любой internal agent request обязан пройти agent authentication, task ownership и assignment-to-seller checks.
- Запрос объекта выполняйте составным условием `id + seller_id`; проверка одного ID недостаточна.
- Область сущности всегда типизирована: `/products/<id>` означает `Product`, а страницы импорта/поставщика — `ImportedProduct`; числовой ID без `entity_kind` неоднозначен.
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
- Для `ImportedProduct` используйте `AgentChangeSnapshot`; для основной `Product` — `CardEditHistory` с `user_comment=agent_task:<task_id>`. Оба пути входят в task-tree rollback и должны тестироваться полным циклом запись → откат.
- Rollback task tree выполняйте только после остановки его активных задач, иначе поздний worker может повторно записать данные.
- Не запускайте destructive workflow из неоднозначного текста: semantic planner возвращает clarification или план, а пользователь подтверждает write-plan до старта.

### Least privilege и достоверность

- Skill получает минимальный `tool_allowlist`; read-only skill не должен иметь update tools.
- LLM не является источником истины для seller/product identity, разрешений, цен, остатков, сертификатов, состава и иных непереданных фактов.
- Сохраняйте inference policy и confidence. Validate structured output и tool arguments перед side effects.
- Не логируйте chain-of-thought, секреты и полные sensitive payloads. UI показывает проверяемые шаги и результаты.

### Характеристики WB из данных поставщика

- Любой supplier/AI characteristic patch перед созданием или обновлением карточки проходит fail-closed проверку по WB category schema из `MarketplaceCategoryCharacteristic` и синхронизированным в админке словарям. Для материала (включая точные WB-aliases `Состав|Состав изделия|Состав материала`) и точных характеристик `Пол|Пол товара` обязателен category-scoped `dictionary_json`, редактируемый администратором: глобальный `kinds` может содержать `Унисекс` и не доказывает допустимость значения для конкретной категории. Для `Цвет`, `Страна производства`, `Сезон` и НДС используйте соответствующий глобальный `MarketplaceDirectory`; ТНВЭД запрашивается только с typed `subjectID`.
- Отсутствующая категория, схема или обязательный словарь блокирует отправку характеристик в WB с диагностикой о синхронизации. Нельзя молча пропускать невалидное значение и сообщать об успешном обновлении.
- В apply-path допустима только точная case-insensitive канонизация значения из словаря. Fuzzy/substring matching можно показывать как suggestion, но нельзя автоматически применять перед side effect.
- Обновление характеристик является patch по `charc_id`: проверенные канонические значения мержатся с полным текущим массивом, чтобы частичное обновление не удалило остальные характеристики. Повреждённый текущий JSON блокирует запись; snapshot/history хранит именно фактически записанный merged JSON.
- Центральные single/create/batch методы WB API повторно проверяют characteristic patch и все уже находящиеся в full-card известные dictionary-bound значения. `subjectID`, patch `id` и ID удаляемых при rollback характеристик принимаются только как positive JSON integer: boolean, float и numeric string не канонизируются через `int(...)`. Batch принимает только full-card с opaque fetched-source receipt, подготовленную поверх свежей карточки WB: prepared context привязан к `nmID`, typed `subjectID`, ID изменённых/удалённых характеристик и fingerprint финального массива, а перед HTTP удаляется. Raw batch, перенос контекста между карточками и неявная потеря untouched-характеристик при нормализации запрещены. `null`/объект не может заменить массив, а пустой patch не очищает существующие значения. Supplier flow до HTTP коммитит durable pending `CardEditHistory` с точными fresh-before/sent-after; неоднозначный transport error сверяется с live WB, а неразрешённая `uncertain|pending`-запись становится доступна для conflict-aware rollback после 5 минут. Фото записываются отдельно и не отключают rollback контента. Rollback конфликтный, tenant-scoped и идемпотентный для одной или цепочки history-записей при повторе после сбоя локального commit. Supplier bulk принимает не более 200 уникальных positive integer Product IDs и переиспользует request-level schema/dictionary cache.
- Ручные category allowlists из админки не должны стираться, когда официальный WB schema response не содержит `dictionary`. Их изменение обновляет version/hash схемы. Rollback характеристик также повторно проходит актуальные schema/dictionary checks; устаревший или недопустимый snapshot не отправляется в WB.
- Не выводите пол или материал из одной категории и не задавайте общий fallback `Пол=Унисекс`. Значение должно прийти из проверяемых данных и пройти словарь WB.

### Актуальность справочников WB

- Категории, характеристики и специальные справочники из админки являются structured truth. Агент читает их typed SQL/internal tools; не переносите эти данные в RAG, prompt constants или память модели.
- Общий refresh категорий и глобальных справочников выполняется каждые 24 часа и через 90 секунд после старта scheduler. Для включённых категорий recovery refresh запускается через 180 секунд после старта с лимитом 200, затем refresh-ahead выбирает схемы старше 30 часов пакетами до 50 каждые 6 часов. Эти jobs разделены и не должны одновременно синхронизировать один schema batch. Hard TTL для agent read/write равен 48 часам: после него данные не используются до успешной синхронизации.
- Синхронизация применяет изменения только после полного типизированного upstream snapshot с успешным top-level WB envelope. Пустой/невалидный ответ, `error=true`, повтор страницы, дубли ID/значений или аномальное уменьшение snapshot не должны снимать availability либо затирать последний успешный кэш. Category `subjectID`/`parentID`, schema `subjectID`/`charcID`/`charcType`/`maxCount` принимаются только как JSON integer без coercion из boolean, float или string; category availability flags и обязательные schema flags `required`/`popular` принимаются только как JSON boolean. Опциональные schema flags `hasFilter`/`isVariable`/`existNamedField` также обязаны быть boolean при наличии, но их отсутствие в официальном ответе нормализуется в безопасный `false` для хранимых полей. Schema response обязан содержать точный запрошенный `subjectID` и typed characteristic fields; `colors`, `countries`, `kinds`, `seasons` и `vat` валидируются по своей официальной форме до записи. Необязательные display-метаданные WB вроде `colors.parentName` могут быть `null`/пустыми и нормализуются, но канонические ID/`name` остаются обязательными и строгими.
- Удалённые WB категории и характеристики не удаляются физически: помечайте их `is_available=false`, сохраняйте историю/настройки администратора и исключайте из новых agent choices. Обновляйте `last_seen_at`, status/error, version и hash; изменение имени, типа, обязательности, `hasFilter`, единицы, лимита или словаря считается новой версией схемы.
- Пользовательские `ai_instruction` не перезаписываются синхронизацией. Для этого хранится явный source `generated|custom`; автоматически регенерируются только generated instructions.
- WB `required=true` сильнее старого admin-флага `is_enabled`: ставшая обязательной характеристика автоматически включается обратно, всегда выдаётся агенту и не может быть отключена вручную.
- Internal reference endpoints возвращают `reference_status`. Категоризация, заполнение характеристик и нормализация размеров обязаны сделать zero-LLM preflight и вернуть проверяемый partial/clarification при `usable=false`. Схема характеристик usable только при свежем общем каталоге и `is_available + is_enabled + is_leaf` у категории. Write endpoints повторно валидируют freshness, availability, точные имена/ID, типы и словари, поэтому prompt-инструкция не является safety boundary.
- Schema endpoint отдаёт для каждой характеристики компактный `constraint` из того же validator-resolver: `source`, `usable`, `count`, не более 40 `values` и `truncated`. Весь global directory JSON в prompt не передаётся. Непроверяемое required-поле делает всю схему `usable=false` до LLM; optional-поле с `constraint.usable=false` пропускается.
- Batch prefetch категорий и схем использует authenticated `/internal/v1/categories/search-batch` и `/internal/v1/categories/characteristics-batch` с лимитом 1..200: один request делает bounded bulk SELECT, сохраняет порядок входа и возвращает typed fail-closed item для каждого query/`subjectID`, включая missing/stale/unavailable.
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

- User route не вызывает Ozon напрямую. Разрешённый путь: seller scope → fresh full draft validation → whitelist builder → committed `MarketplaceOperation` + `MarketplaceListingSnapshot` → live absence preflight → quota reservation → adapter write → task polling/reconciliation.
- P5a создаёт только отсутствующий `offer_id`. Найденный upstream offer, включая archived, завершает operation ошибкой до quota/write; update требует отдельного full-state workflow и не маскируется create path.
- `submitting` и `attempt_count > 0` выставляются и commit-ятся до вызова write adapter. Transport/5xx/malformed success после этого считается ambiguous; повтор `/v3/product/import` запрещён. Definitive validated API rejection может стать `failed`.
- Poll/status response обязан совпасть exact-set по offer, иметь bounded items/errors и известные statuses. Неполный, foreign, duplicate или malformed response не считается успехом. После 24 часов неудачного task polling automatic retry прекращается с видимым `uncertain`; ручной poll остаётся возможен.
- Live reconciliation без task id разрешена только когда committed before-state доказывает отсутствие offer до write. Она не выполняет новый write. `uncertain` сохраняет quota reservation и credentials до подтверждения либо явной будущей recovery-процедуры.
- Выключение `MARKETPLACE_OZON_PUBLICATION_ENABLED` запрещает новый write и отправку безопасной queued operation, но не отменяет уже начатую сверку. Disconnect не может удалить ключ, нужный для reconciliation.
- Rollback snapshot обязателен, но автоматическая archive-команда остаётся `unavailable` до официально подтверждённого и покрытого contract fixtures endpoint. UI не должен обещать кнопку отката, если upstream compensation не реализована.

## LLM policy, budgets и prompt cache

- Новая seller AI-настройка создаётся с `provider=deepseek`, primary model `deepseek-v4-pro` и `agent_single_model=false`. Orchestration task types `plan_request`, `smart`, `custom`, `pipeline` используют seller primary model, обычно DeepSeek Pro.
- Internal execution skills используют DeepSeek Flash с `thinking.type=disabled`, если `agent_single_model` не включён. Pro-планирование сохраняет provider-default thinking. Никогда не переносите key/base URL между providers при fallback/model switch.
- Seller-scoped AI profile имеет приоритет. Credentials передаются task-scoped через authenticated internal API и не записываются в логи.
- Default budgets определены в `agents/config.py`: `AGENT_RUN_TOKEN_BUDGET=30000`, `AGENT_RUN_API_BUDGET=24`, `AGENT_MAX_PRODUCTS_PER_RUN=200`, `AGENT_OBSERVATION_MAX_CHARS=1200`. Изменение defaults требует тестов и обновления этого файла.
- При исчерпании budget возвращайте честный partial result без дополнительного LLM call. `llm_retry` считает каждую физическую попытку в `usage.api_requests`, а execution-path ограничивает retries фактическим остатком API-бюджета; параллельные чанки заранее делят общий лимит и не могут превысить его суммарно. Сохраняйте cancellation checks и durable skill-boundary checkpoints.
- Для больших наборов используйте prefetch, bounded chunks, batch endpoints и bounded concurrency. Не создавайте N+1 DB/API/LLM calls.
- Явно названные поля контента извлекаются детерминированно в immutable mask `title|description`: semantic planner не может расширить эту маску. `content-writer` принимает максимум 100 typed positive integer IDs без coercion из boolean/float/string, делает один content-brief query, затем Flash chunks: до 24 карточек для title-only и до 8 для description/both, дополнительно ограничивая prompt примерно 12 000 символов. Каждый чанк обязан вернуть точное множество уникальных integer IDs и все поля; stop-word response обязан полностью совпасть по `(product_id, field)`. Любой пропуск, дубль, чужой ID или неполная проверка блокирует запись чанка без LLM retry/ReAct fallback. Product/ImportedProduct сохраняются batch endpoint-ами с optimistic `expected_updated_at`, snapshots/history и честными changed/unchanged/failed counts.
- Cancellation проверяется до prefetch, до и после каждого LLM chunk, перед postprocess/tool/write и перед commit. После отмены не планируйте новые futures и не исполняйте уже сгенерированные tool calls. Structured batch error не должен автоматически переключаться на дорогой ReAct; usage сохраняется в partial/failed result и checkpoint.
- Не запускайте LLM-классификатор перед точным запросом. Regex/enum/typed SQL остаются первым уровнем; после deterministic miss любой непустой запрос маршрутизируется одним `plan_request` на seller primary model (обычно Pro) с компактным стабильным capability catalog и output cap 1200 токенов. Python повторно валидирует typed scope, skill allowlist, параметры и risk, игнорирует model-reported risk и не разрешает semantic plan расширить запрет на writes. Semantic write без выбранных IDs допустим только после typed supplier selection либо при явной фразе о всём каталоге; модель не может сама расширить узкий запрос до всех товаров. Для semantic `catalog-query` не выполняется отдельный Flash polish: planner + typed SQL остаются одним LLM-вызовом. Usage planner переносится в execution run и учитывается в общем API/token budget.
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
