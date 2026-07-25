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
- `services/competitor_monitor.py`, `services/competitor_fetch.py`, `routes/competitors.py`: мониторинг конкурентов v2. Никаких собственных тредов: singleton scheduler раз в минуту синхронизирует до 2 due-продавцов (`next_sync_due_at`, интервал 30..1440 минут per seller, default 60). Fetch-слой ORM-free: basket CDN для метаданных (404 = товар удалён), current `catalog.wb.ru/sellers/v4/catalog` по продавцу и search v18 по бренду для цен, а exact `card.wb.ru/cards/v4/detail` bounded batch-ами до 100 nmID наблюдает публичную пару цен наших карточек за уже принятыми exact-match (до 300 карточек за seller-sync); ОДИН глобальный process-wide rate limiter (`COMPETITOR_PUBLIC_RPM`, default 20) на все публичные вызовы всех продавцов + circuit breaker per источник (3 подряд 429/5xx -> cooldown 10 минут). Публичная пара всегда coherent из одного размера: `basic` — цена до скидок, финальная витринная — `total`, если поле присутствует, иначе current `product`; seller API `discountedPrice` не подставляется как покупательская цена, потому что не включает скидку WB. 429 никогда не ждётся sleep-ом. Fetch-miss — не наблюдение: current-значения не затираются, снимок не пишется, `price_miss_count` растёт и включает basket-recheck; miss нашей публичной цены также не затирает последний факт. Снимок цены создаётся только при успешном наблюдении с фактическим изменением; цены конкурентов хранятся в рублях (integer), публичная пара своей карточки — в `Product.wb_public_base_price/wb_public_final_price` с freshness. HTTP-пути bounded: добавление товаров — только вставка nm_ids (строгая typed-валидация, cap 300) с `next_sync_due_at=now`; интерактивный поиск/превью каталога — одна страница (превью продавца hard-cap 100), использует настроенный seller proxy только server-side, а 429 возвращает честный 503 с машинным `code=wb_rate_limited`. При таком 503 overlay может сделать credential-free CORS GET current seller v4 прямо из браузера пользователя (также одна страница/100), но на сервер из этого preview отправляются только выбранные exact nm_ids: цены и metadata браузера не становятся сохранённым наблюдением. Seller-first overlay в `templates/competitors_group_detail.html` + `static/competitors-group.*` принимает ID/ссылку WB, фильтрует и выбирает товары; ответ insert-only POST возвращает bounded pending-строки для немедленного локального списка. Действие «Добавить каталог» может browser-assisted получить до 3 страниц/300 nm_ids, вставить их exact POST и снять заявку после наблюдённого конца либо hard cap; при partial/CORS/429 остаток остаётся durable-заявкой `import_requested`, которую выполняет scheduler (до 3 страниц за тик, до 300 товаров на группу). Seller-scoped DELETE заявки не удаляет уже добавленные товары. Пустая первая серверная страница считается fetch-miss и не снимает заявку, пока не наблюдён хотя бы один товар. `proxy_url` — опциональный credential: запись fail-closed шифруется Fernet, наружу только маска `proxy_display()`. Алерты зеркалятся одним агрегированным Notification (дедуп 4 часа). Компакция снимков — чанковая (5000 строк/commit) без длинного SQLite write-lock; исторический v1-мусор чистит идемпотентная `migrate_compact_competitor_snapshots.py` с бюджетом времени на прогон, схема публичных цен — `migrate_add_competitor_price_lanes.py`; миграции подключены fail-fast в entrypoint и comprehensive runner.
  - Pending seller import после 429/пустой первой страницы получает честный `waiting_wb` без красного `last_sync_error` и `next_sync_due_at` через 10 минут, а не обычный час. При открытии detail-страницы уже явно запрошенная `import_requested` заявка один раз автоматически пробует browser-assisted resume; без сохранённой заявки page load ничего не импортирует.
  - Server-side retry продолжает seller catalog со следующей сотни (`existing_count // 100 + 1`, total page cap 3), потому что WB anti-bot может пропускать только одну страницу за cooldown; уже вставленные nm_ids остаются идемпотентными, общий cap группы 300 не расширяется.
  - `services/competitor_matching.py` — общий для всех продавцов контур идентичности `WB nmID → SupplierProduct`. Единственная глобальная строка `CompetitorProductMatch` на nmID хранит shared deterministic/LLM suggestion и cache fingerprint; продавец не может её переписать, его confirm/reject/alternate choice живёт отдельно в `SellerCompetitorMatchReview` и append-only `CompetitorMatchEvent`. Match evidence с WB берётся только из наблюдённых whitelist-полей `CompetitorProduct`, а кандидат при наличии snapshot — исключительно из whitelist исходного `SupplierProduct.original_data_json` (`title/description/brand/category`, source characteristics/dimensions/variants/barcodes/photo URLs), без per-field fallback в нормализованные колонки; whole-row legacy fallback разрешён лишь строкам без snapshot. `Product`, контент `ImportedProduct`, `processed_photos*`, все `ai_*`, marketplace suggestions и seller edits запрещены как identity evidence и не отправляются модели. Seller price появляется после матча только по точной цепочке `SupplierProduct.id → ImportedProduct(seller_id, supplier_product_id, product_id) → Product(seller_id, id)` и никогда не влияет на общий score. Legacy-пустой `ImportedProduct.supplier_product_id` разрешено восстанавливать только точным уникальным source-key `(supplier_id, external_id)` через чанковую fail-fast `migrate_backfill_imported_supplier_links.py`; title/barcode/fuzzy backfill запрещён.
  - Matching HTTP только создаёт bounded `BackgroundJob(job_type=competitor_matching, cap 300)` или пишет seller-local review; фото/LLM вызовов в request нет. Singleton scheduler раз в минуту обрабатывает одну очередь, default до 3 nmID/тик и 45 секунд. Сначала process-cached supplier text index строит top-3 shortlist, затем сравниваются до 3 исходных фото с каждой стороны локальным perceptual hash; после этого допускается ровно один physical structured LLM-call на карточку (`llm_retry_attempt_limit(1)`); для этого короткого классификатора DeepSeek thinking явно выключен, чтобы reasoning не съедал JSON output budget. Глобальный nmID claim и повторная проверка fresh fingerprint выполняются atomic compare-and-set до image/LLM I/O, поэтому конкурентные seller jobs не дублируют физический вызов. LLM-verdict `same` не является границей exact identity: Python-owned gate требует либо strong photo + достаточное text evidence, либо near-exact title/brand evidence, запрещает brand/numeric/LLM-reported conflict и иначе понижает результат до `uncertain` для seller review. Legacy shared `same` без сохранённого gate допускается в exact-сводку только при одновременно высоких final/text/photo score; seller-confirmed `same` остаётся явным override. `evaluation_fingerprint` включает версию алгоритма, observed WB identity facts, только whitelisted identity fingerprints top-кандидатов и provider/model; supplier price/quantity/RRP и другие commercial-поля в fingerprint не входят. Completed результат переиспользуется между продавцами 30 дней, поэтому повторный seller и коммерческое обновление фида не вызывают фото-host/LLM. Stable system/schema prefix сохраняет provider prompt caching; raw provider bodies/errors не хранятся, ValueError сворачивается только в allowlisted safe error code. Бюджеты: `COMPETITOR_MATCH_ITEMS_PER_TICK=3` (1..10), `COMPETITOR_MATCH_TICK_SECONDS=45` (10..55), `COMPETITOR_MATCH_LLM_MAX_PER_JOB=300` (0..300), `COMPETITOR_MATCH_RECHECK_DAYS=30` (1..365), `COMPETITOR_MATCH_INDEX_TTL_SECONDS=600` (60..3600), `COMPETITOR_MATCH_MAX_PHOTOS_PER_SIDE=3` (1..5), `COMPETITOR_MATCH_IMAGE_MAX_BYTES=8388608` (256 KiB..20 MiB). UI «Соответствия товаров» в detail группы явно маркирует общий кэш, source-only evidence и индивидуальную цену. Схему добавляет идемпотентная fail-fast `migrate_add_competitor_matching.py`, подключённая к entrypoint и comprehensive runner.
  - `services/competitor_comparison.py`, `GET /api/competitors/comparison` и UI `/competitors/comparison` — read-only seller-scoped сводка «все отслеживаемые продавцы против нас» либо одна выбранная группа против нас. Она не сопоставляет competitor cards попарно и не вызывает WB/image/LLM: completed identity rows агрегируются по effective `SupplierProduct.id`, seller-confirmed `same` сильнее Python-admitted shared suggestion, rejected/weak-legacy/analog/uncertain/different исключаются. Одинаковый nmID не считается дважды. Две ценовые линии никогда не смешиваются: `base` сравнивает нашу публичную `basic` (до первого наблюдения допустим seller base fallback) с competitor `basic`, `final` — только нашу публично наблюдённую `total|product` с competitor `total|product`; seller API `discountedPrice` показывается лишь справочно и не участвует в final position. Для каждой линии отдельно считаются минимум, медиана, разброс, позиция и gap. Own card доступна только через exact import FK. HTTP bounded до 100 active groups, 1000 offers, 50 canonical товаров на страницу; ответ явно сообщает truncation, identity/source scopes и price contract.
- `services/marketplace_adapters/`: типизированный registry и provider adapters. Adapter не принимает ORM objects и не выполняет tenant authorization.
- `services/ozon_api_client.py`: строгий Ozon Seller API transport с endpoint allowlist и разными retry-классами для read POST и write POST.
- `services/marketplace_accounts.py`, `routes/marketplace_accounts.py`: seller-scoped кабинеты маркетплейсов, encrypted credentials и read-only connection checks.
- `services/marketplace_reference_accounts.py`: отдельный admin-owned credential lifecycle только для глобальных Ozon-справочников.
- `services/ozon_reference_service.py`: strict category/type/attribute/value snapshots, freshness, shrink guards, admin restrictions и два раздельных контура доступности. Все official available типы по умолчанию `is_seller_selectable=true`; legacy/admin `is_enabled` означает только proactive refresh-ahead и не скрывает тип от продавца. Minute scheduler on-demand выбирает до 3 exact типов, уже referenced активным draft/mapping/listing, и до 6 их словарей за тик; неиспользуемые тысячи типов не образуют API backlog. Failed schema/value scope повторяется не раньше чем через 10 минут, без sleep/429-loop. Схему добавляет fail-fast `migrate_add_ozon_product_type_visibility.py`.
  - Entrypoint/comprehensive runner исторически вызывает base `migrate_add_ozon_references.py` перед отдельной visibility-миграцией. Поэтому base migration обязана additive-добавить legacy-missing `marketplace_product_types.is_seller_selectable` до создания selectable index; менять этот порядок без backward-compatible prerequisite запрещено. Отдельная visibility migration затем остаётся идемпотентной и выставляет NULL legacy rows в `1`.
- `services/marketplace_listings.py`, `routes/marketplace_listings.py`: seller-scoped unified listing read model, resumable Ozon catalog sweep и marketplace/account-filtered UI/API.
- `services/marketplace_source_identity.py`, `services/marketplace_product_links.py`, `services/marketplace_source_link_reconciliation.py`: audited exact matching между общей seller-owned `ImportedProduct` и WB/Ozon listing-проекциями. Помимо literal offer/vendor identity, Python-owned anchored parser снимает только доказанные обёртки ID поставщика: Сексоптовик `id-<source>-<seller>` и legacy `S`, Андрей `...A<external>`/legacy wrapped serial, `V` — только exact vendor code. Legacy `K/L` имеют отдельные пересекающиеся числовые namespaces и без отдельного source registry намеренно не парсятся. Нормализованный source-key обязан быть уникальным в своём supplier scope; title/category/barcode/LLM/fuzzy/transliteration не участвуют. Если canonical-копии ещё нет, она materialize-ится без provider call только по уникальной тройке `connected SupplierProduct + seller-owned WB Product + Ozon listing`; существующая конфликтующая WB/supplier связь никогда не перезаписывается. Явный seller unlink не подхватывается автоматикой, но ручной reconcile может снова применить exact identity. Durable `BackgroundJob(marketplace_source_link_reconcile)` keyset-ом обрабатывает существующий backlog: до 3 Ozon account scopes и 200 listings за минуту, хранит cursor/counters, безопасно resume-ится и пересматривает unresolved не чаще чем раз в 6 часов.
  - Массовая/одиночная подготовка нового Ozon draft может без повторного клика активировать seller-scoped `MarketplaceCategoryMapping(mapping_source=deterministic)` только из уже опубликованных exact-linked карточек того же продавца. Ключом служит существующая точная identity категории (`WB subject id` при подтверждённой WB-проекции, иначе supplier/source scope + нормализованная точная категория); title/LLM/fuzzy не участвуют. Требуются минимум 2 текущих неархивных listing с `link_source in {exact_source_identity, exact_offer_identity}`, ноль карточек с отсутствующим/несвежим типом, один и тот же доступный product type у всех наблюдений и fresh official Ozon schema. Consensus v2 дополнительно делает bounded negative-only scan до 50 000 seller-owned `ImportedProduct`: несколько разных positive `wb_subject_id` внутри одной supplier/source-категории доказывают её структурную неоднородность и запрещают category-wide auto-mapping, но сами эти неподтверждённые subject hints никогда не выбирают Ozon type. Один пример, любой конфликт, неоднородная source-категория, неизвестный тип, stale schema, non-exact/manual link либо неполный bounded scan не создаёт mapping; поздний конфликт переводит только автоматический mapping в `stale`. Manual/rejected/corrected mapping никогда не перезаписывается. Scan сериализован seller-lock, fail-closed ограничен 20 000 listings и 50 000 canonical products, не вызывает provider/LLM и выполняется один раз перед bulk-run; UI явно маркирует категорию как подтверждённую уже связанными карточками Ozon.
- `services/marketplace_fact_pack.py`: marketplace-neutral observed facts с field-level provenance; legacy AI output остаётся отдельным unverified suggestion. Fact-pack v2 отдельно сохраняет observed supplier `source_title`, bounded `all_categories` и literal `sizes_raw` (как `attributes.sizes.raw` с точным provenance), не смешивая их с seller-current title, нормализованным/AI size container либо legacy `ai_*`; это позволяет точным channel-mapper-ам использовать буквальные признаки исходной категории и размера, не выдавая AI за факт. Linked WB projection сравнивается с canonical только по bounded common content (`title/description/brand`): drift виден в Ozon readiness/validation, но никогда автоматически не перезаписывает master.
- `services/marketplace_canonical_content.py`: reviewed Ozon → canonical diff только для `title/description`; fresh exact-account snapshot, optimistic drift gate, seller-owned reviewer и conflict-aware local rollback обязательны. Provider calls и перенос channel/reference IDs в этом сервисе отсутствуют.
- `services/marketplace_listing_media.py`: exact seller/account/listing target для Image Lab и других media-потоков. Исходные фото остаются у canonical `ImportedProduct`; Ozon projection отдаёт только bounded fingerprint/count и channel constraints, но не становится вторым media master.
- `services/infographic_campaigns.py`, `routes/infographic_campaigns.py`: seller-scoped массовые кампании `/image-lab/campaigns` по точной выборке до 200 `ImportedProduct`; durable items/slides, source-drift gate и human review. Fact-safe v2 строит hero + до четырёх сгруппированных exact fact cards на слайд, удаляет точные semantic-дубли, берёт байты фото через canonical Image Lab cache/candidate fallback и композит original RGB локально. Этот контур не вызывает marketplace write и не меняет порядок галереи WB.
- `services/marketplace_media_channels.py`, `services/marketplace_media_publications.py`, `routes/marketplace_media_publications.py`: marketplace-neutral durable preview/confirm/publication/reconciliation/rollback для проверенных media artifacts. WB реализован через exact live gallery и single-attempt replace; Ozon хранит точный `account + listing` target, но provider write fail-closed выключен до интеграции с его full-state publication lifecycle. Публичные изображения выдаются только по короткоживущим подписанным URL.
- `services/supplier_catalog_enrichment.py`, `routes/supplier_catalog_enrichment.py`: admin-only durable массовое обогащение общей `SupplierProduct` на `/admin/suppliers/<id>/catalog-enrichment`; модель выбирает только из Python-owned fresh WB leaf candidates, спорные строки требуют review, а provider write отсутствует. `migrations/migrate_add_supplier_catalog_enrichment.py` добавляет run/item journal и supplier-content revisions.
- Приём CSV-фидов поставщиков живёт в `SupplierCSVParser` (`services/supplier_service.py`): header-based `csv_column_mapping` поддерживает merge-`list` из нескольких колонок (первая колонка приоритетна, для `categories` — новое дерево + legacy), РРЦ-fallback (`recommended_retail_price_fallback`), `video_url`, полный список штрихкодов (`SupplierProduct.barcodes_json`, legacy `barcode` хранит первый), габариты как dict `{имя: значение}` и opt-in `_include_unmapped`: несмаппленные колонки заголовка сохраняются в `raw_extra`. `SupplierProduct.original_data_json` — наблюдённые данные поставщика, освежается каждым sync и при импорте/«Обновить карточки» копируется в `ImportedProduct.original_data` (observed source для marketplace fact pack). РРЦ и полный список ШК доезжают до `ImportedProduct` (`recommended_retail_price`, `barcodes`); маппинг поставщика `andrey` обновляет `migrations/migrate_andrey_feed_full_ingest.py` (fail-fast в entrypoint). Seller detail `/supplier-catalog/<supplier>/products/<product>` показывает bounded характеристики и габариты из фида отдельно от AI-parsed/marketplace suggestions; AI-поля не выдаются за observed source. Ингест характеристик перестраивает `characteristics_json` целиком по каждому свежему парсу (условная перезапись оставляла загрязнённые данные навсегда), dimension-образные имена («Ширина/Длина/Высота упаковки, см», «Вес упаковки, кг») детектором `wb_content_payload` переезжают в `dimensions_json` под исходными именами фида; исторические строки чистит идемпотентная `migrations/migrate_clean_characteristic_dimensions.py` (fail-fast в entrypoint), staging-копии `ImportedProduct.characteristics` разводит runtime-фильтр.
- `services/wb_card_audit.py`: WB-ревизия — live-сверка до 200 seller-owned карточек с WB (`fetch_cards_by_nm_ids` + `/content/v2/cards/error/list`): существование, фото, какие характеристики реально лежат на WB и чем отличаются от локальных. Только read-вызовы; контент-поля не перезаписываются — расхождения фиксируются в `Product.wb_audit_json`/`wb_audited_at` (миграция `migrate_add_wb_card_audit.py`, fail-fast). Исключение — `Product.photos_json`: как проекция галереи WB он зеркалит live-факт, включая честный пустой список (полный синк каталога больше не фабрикует «стандартные 5 фото» при нуле). Основной триггер — bulk-кнопка «Сверить с WB» в «Моих товарах» (`POST /my-products/wb-audit`, фоновый BackgroundJob); supplier enrichment не выдаёт немедленный audit за подтверждение асинхронной записи, для него действует отдельная durable reconciliation ниже. Результат виден бейджами в «Моих товарах» и блоком «Реальное состояние на WB» в wb-preview.
- `services/wb_enrichment_merge.py`, `services/supplier_enrichment.py`, `services/wb_enrichment_reconciliation.py`: все seller-facing пути одиночного, legacy `/my-products/<id>/wb-enrich` и `/my-products/<id>/wb-reupload-photos`, массового, supplier-update-hub и supplier/photo-действия экрана «Качество карточек» используют один preserve-live контракт. `services/card_improver.py` оставляет только read-only proposal helpers; его старые `apply_*` — fail-closed compatibility shim без `cards/update`/`media/save`. Content merge строится только по свежей live-карточке: характеристика после category/dictionary validation добавляется при отсутствующем/пустом `charc_id`, а непустую заменяет лишь строго более длинный semantic superset с действительно новым токеном и без потери live-токенов; повторы/пунктуационная «длина», numeric-факты и введённое отрицание/исключение не считаются улучшением. `title/description` следуют тому же правилу, непустой `brand` никогда не меняется автоматически, `dimensions` заполняет только отсутствующие/пустые ключи, неизвестные поля fail-closed запрещены. Preserve-live update-normalizer не фабрикует недостающие габариты из дефолтов и не превращает unrelated update в скрытое удаление legacy-характеристики: неполный/непригодный live-state останавливает запись. Full-replacement охвачен seller content lock и тремя exact wire-equivalent live-read (первичная стабильная пара и финальный preflight после durable receipt); любой наблюдённый drift останавливает отправку. Под тем же lock новый enrichment-write для карточки не начинается, пока её предыдущий content receipt активно ожидает reconciliation в `pending|submitted|uncertain|partial`; terminal `partial` с очищенным due не блокирует новый live-план. Durable bulk cursor остаётся на строке и строит новый live-план только после terminal reconciliation. HTTP transport никогда автоматически не повторяет `POST`/`PUT` и не следует write-redirect: write этого контура без provider idempotency key всегда single-attempt, а 3xx/5xx/timeout после отправки считается uncertain и уходит только в read-only reconciliation; автоматический transport retry разрешён лишь для `HEAD/GET/OPTIONS`. WB не даёт revision/ETag/CAS, поэтому невидимая внешняя правка в остаточном интервале final-read → provider apply принципиально неустранима, но локальные enrichment-конкуренты сериализованы и ни один наблюдённый drift не перезаписывается.
  - Read-only proposal экрана «Качество карточек» также требует ровно одну seller-scoped строку поставщика: её отсутствие возвращает явный `409 code=supplier_source_unavailable`, а не внутренний `500` и не предложение, которое затем невозможно безопасно применить.
  - Wire-equivalent fingerprint трёх content live-read канонизирует только порядок массива `characteristics` по `charc_id`, потому что WB возвращает один набор в нестабильном порядке; фактический latest payload не сортируется. Любое изменение id/value, scalar-поля, dimensions, sizes или другой wire-семантики остаётся drift и блокирует write.
  - Локальный full-card validator перед `cards/update` возвращает в durable job/result конкретные bounded allowlisted причины (например, отсутствующий положительный `dimensions.weightBrutto`), а не общий `Validation failed`; при такой ошибке provider write не выполняется. Нулевой/отсутствующий live-вес нельзя заменять захардкоженным дефолтом: нужен фактический вес упаковки в килограммах. Exact supplier snapshot `ImportedProduct.original_data.dimensions` проходит общий dimension extractor и имеет приоритет над staging/AI; положительный observed candidate может заполнить невалидный live `0`, но валидный положительный live-факт по-прежнему не перезаписывается.
  - `services/wb_package_dimensions.py` — единственный дополнительный допустимый источник габаритов упаковки: факт, явно заявленный самим продавцом в `ProductDefaults` (`/settings/product-defaults`), который путь создания карточки использует с самого начала. `resolve_declared_package_dimensions` отдаёт ТОЛЬКО заявленные значения активных правил (категорийное правило перекрывает глобальное по каждому ключу отдельно) и никогда не подмешивает захардкоженные `10×10×5/0.1` из `get_defaults_for_product`; неактивное правило заявлением не считается, а ошибка чтения гасится в пустой результат и приводит к честному отказу. `plan_declared_dimension_repair` чинит только ключи, ПРИСУТСТВУЮЩИЕ в живой карточке WB и невалидные там: валидное положительное живое значение не перезаписывается, отсутствующий ключ не фабрикуется, а ключ без заявленного факта попадает в `unresolved_keys` и блокирует запись. Заявленное значение уже в целевых единицах (см и кг) и не проходит supplier-эвристики `wb_content_payload` про граммы/миллиметры; вес округляется до трёх знаков, линейные габариты — до целых, и значение, обнулившееся при округлении, фактом не считается. Preserve-live ветка `update_card` применяет починку только когда запись уже запрошена (сам ремонт не инициирует новый WB write), проводит её через тот же merge-план и пишет провенанс в `merge_decisions.declared_dimension_repair`; legacy generic `update_card`, `update_cards_merged` и `update_cards_batch` делают то же самое и при `unresolved_keys` не отправляют карточку. Кеш заявленных значений живёт 60 секунд и сбрасывается в `invalidate_product_defaults_cache`.
  - Ни один update-путь больше не подменяет невалидный габарит захардкоженным `DEFAULT_DIMENSIONS`. `normalize_update_card_payload` при `fill_missing_dimensions=True` по-прежнему добирает ОТСУТСТВУЮЩИЙ ключ, но присутствующее непригодное значение сохраняет как есть, а `prepare_card_for_update` больше не удаляет невалидный `weightBrutto`: удаление было тихой мутацией, после которой карточка уходила в WB вообще без веса, а локальная валидация молчала, потому что проверяет вес только при его наличии.
  - Фото не имеют destructive enrichment replace: `smart_merge` и legacy `replace/append` сохраняют все live-слоты и порядок, bounded WB thumbnails и локальные source-файлы сравниваются perceptual hash, совпадения/дубли пропускаются, новые фото идут только с `live_count + 1` до лимита 30. Поскольку общий photo cache нормализует source в квадрат 1200×1200, для matching/preflight/reconciliation приоритетен WB `square`; портретные `tm`/`big` остаются только bounded fallback и не должны создавать ложный конфликт из-за смены aspect ratio. Любой fallback `Product → ImportedProduct → SupplierProduct` обязан дать ровно одну seller-scoped строку; несколько exact/legacy-кандидатов считаются неоднозначностью и блокируют enrichment, а не выбираются через `.first()`. Точная свежая `SupplierProduct`-галерея, включая пустую, является авторитетной относительно staging-копии `ImportedProduct`; битая/mismatched exact-ссылка блокирует фото fail-closed. Если импортёр находит уже существующий `nmID`, его `_link_existing_card` сначала фиксирует exact local FK и использует тот же append-only enrichment, а post-create slot-1/media-save uploader разрешён только новым карточкам. Если хотя бы один live-thumbnail нельзя доказанно сопоставить, append fail-closed блокируется. Seller media lock охватывает pending-receipt gate → live read → fingerprint → durable receipt → финальный exact gallery preflight → multipart append; `only_if_empty` также проверяет live. Raw SHA-256 локального source-файла входит в план, повторно проверяется, а multipart отправляет уже зафиксированные в памяти байты; замена cache-файла между plan и POST не пройдёт. Multipart прекращается на первой rejected/ambiguous позиции, не создаёт дыр и хранит, какие слоты приняты, не отправлены или могли примениться. Supplier-update-hub атомарно сериализует active-job check+insert host-shared seller/type lock-ом, а сам `BackgroundJob` дополнительно использует exact JSON compare-and-set generation claim: stale worker после длинного I/O не может продвинуть cursor/counters, если его уже сменил restart-worker. Как и content API, media API не имеет CAS: внешняя правка в остаточном интервале final-preflight → slot POST неустранима, но delayed reconciliation повторно проверяет perceptual prefix и точные appended-позиции, не требует хэша от добавленных позже незатронутых ручных хвостовых фото, заменяет плановые hash-токены в `snapshot_after` фактическими provider URL и зеркалит `Product.photos_json` только по всей наблюдённой live-галерее; conflict/manual drift никогда не исправляется автоматическим write-retry.
  - Каждый рассчитанный field/photo no-op и pre-send план хранится в `CardEditHistory.merge_decisions` вместе с exact before/planned-after; multipart receipt отдельно считает accepted/uncertain/rejected/not-attempted позиции без raw provider body. Provider HTTP acceptance означает `submitted`, timeout/неясный исход — `uncertain`, но не `success`; только bounded read-only scheduler reconciliation через live cards + `/content/v2/cards/error/list` выставляет `success/partial/failed/conflict` и после подтверждения зеркалит content в `Product`. Write автоматически не повторяется ни после timeout, ни после crash. Все seller-facing bulk-входы принимают не более 200 уникальных typed positive `Product.id`; review-экраны с per-card чекбоксами сохраняют exact whitelist полей каждой строки в том же durable `EnrichmentJob`, а worker проверяет, что union и exact ID-set не подменены. Selective `photo_indices` (1..30 уникальных неотрицательных integer), их exact source-range и выбранные source URL проверяются до первого content/media side effect, а supplier-gallery drift между content и media блокирует фото до WB. `EnrichmentJob` хранит exact product IDs, per-row cursor, lease/current-item и bulk receipt; потерявший lease worker не может продвинуть cursor после внешнего I/O, а active pending receipt или занятый seller-lock делает строку `deferred`, не `failed/skipped`. UI различает «отправлено и сверяется» от «новая запись ещё не выполнялась». Singleton scheduler каждые 20 секунд продолжает bounded dispatch и reconciliation после web-thread/container restart. Бюджеты: `WB_ENRICHMENT_ITEMS_PER_TICK=3` (1..20), `WB_ENRICHMENT_TICK_SECONDS=45` (10..55), `WB_ENRICHMENT_LEASE_SECONDS=600` (120..1800), `WB_ENRICHMENT_RECONCILE_ITEMS_PER_TICK=10` (1..50), `WB_ENRICHMENT_RECONCILE_MAX_ATTEMPTS=6` (2..10), `WB_ENRICHMENT_RECONCILE_INITIAL_SECONDS=90` (30..600). Audit-колонку добавляет `migrate_add_enrichment_merge_audit.py`, restart/reconciliation-схему — идемпотентная fail-fast `migrate_enrichment_reliability_v2.py`; обе подключены к entrypoint и comprehensive runner.
- `services/marketplace_drafts.py`, `routes/marketplace_drafts.py`: seller/account-scoped Ozon drafts, exact category mappings, optimistic edits и deterministic publishability validation без provider/LLM calls. `MarketplaceDraftService.bulk_prepare` — детерминированная bulk-подготовка до 200 уникальных positive integer exact seller-owned `ImportedProduct` для одного owned активного кабинета; существующие drafts считаются отдельно, ошибка одного товара не блокирует остальные, неудачная validation созданного черновика не считается failed, provider/LLM не вызываются. Exact source-backed optional Ozon defaults дозаполняются только из observed fact pack и всё равно проходят fresh type-scoped dictionary gate: literal title/color/vendor code, product length/diameter/weight, package weight, узкие TPR/TPE spelling expansions и только доказанные phrase mappings source-category/title (`с/без вибрации`, режимы, особенности, вид насадки/стимулятора/мастурбатора/страпона/BDSM-аксессуара, назначение, размерный диапазон official dictionary). Clothing-рецепт использует только literal `sizes_raw` и явную source clothing taxonomy (RU range разворачивается официальным шагом 2, hosiery `2/3` — exact values; measurement-only строки вроде `50 мл` или `длина 38-40 см` не становятся размером), observed gender/colors и percentage composition; unique source vendor code заполняет required Ozon group key без случайного объединения вариантов. Adult-рецепт выставляет `18+` только из явной source/official adult taxonomy, переводит literal gender лишь в текущие official значения `Унисекс|Для него|Для нее`, aroma `Без аромата` — только из `без запаха|без аромата`, taste — только из literal taste-факта. Свободный hashtag состоит только из source category-theme и нормализованного имени уже выбранного official Ozon type, без brand/title/parameters. Для лубриканта exact source facts могут дать область применения, явно названную основу/material, объём и доказанные эффекты; texture и вкус не угадываются. Маркировка и ТН ВЭД никогда не выводятся из этих рецептов. Fuzzy/LLM/legacy `ai_*` и неподтверждённые free-text выводы запрещены; существующее seller value не перезаписывается. Route `POST /marketplaces/drafts/bulk-prepare` остаётся compatibility/readiness-обёрткой; основной seller-facing вход из «Моих товаров» сразу создаёт единый mass-upload run ниже. Тот же сервисный метод вызывает `SupplierService.import_to_seller(draft_account_ids=...)` для явно выбранных чекбоксами «Каналы» кабинетов в каталоге поставщика (дополнительно к авто-провижинингу auto-publish-кабинетов и независимо от него).
- `services/ozon_bulk_upload.py`, `routes/ozon_bulk_uploads.py`: единый seller-facing массовый flow `/marketplaces/ozon/uploads/` из «Моих товаров» и готовых черновиков. Одно явно подтверждённое действие «Синхронизировать Ozon» создаёт отсутствующие offer и выполняет full-state update уже опубликованных exact-linked карточек; JSON требует literal `confirm_write=true`, form — exact `confirm_write=1`. Обычный экран «Мои товары» bounded показывает до 200 строк, поэтому «Все на странице» использует полный лимит flow без скрытого 40-card ceiling. CTA допускает только active connected кабинет с сохранённым неистёкшим key и явно выбранным `default_vat`; account UI показывает дату истечения. При подтверждённой синхронизации legacy source snapshot только дозаполняет отсутствующие observed photos/dimensions/characteristics/barcodes/RRP из exact `supplier_product_id`; seller-поля не перезаписываются. Затем deterministic three-way rebase переносит свежие source defaults только в не изменённые продавцом поля, сохраняет seller edits/complex groups и считает exact completeness (required/supplied schema, content, media, physical, commercial, publishable), не выдавая процент или догадку за факт. Один durable `BackgroundJob(job_type=ozon_bulk_upload)` принимает до 200 exact seller-owned `ImportedProduct`, локально создаёт/rebase-ит/валидирует drafts и отдельными create/update chunk-ами до 50 создаёт committed `MarketplaceOperation`; HTTP не вызывает provider. Run хранит action и bounded результат каждой карточки (`queued/submitted/checking/succeeded/already_current/needs_input/ready_to_retry/excluded/failed/uncertain`) с нормализованной причиной Ozon/validation, а scheduler после operation reconciliation отражает результат в run. Exact live full-state, уже равный submitted payload, становится успешным `already_current` с `attempt_count=0` и без provider write. Progress document ограничен 512 KiB; в summary остаются две главные bounded validation-причины, полный список — в самом draft. Restart восстанавливает потерянную ссылку по exact seller+draft operation; prewrite `preparing|queued` без durable operation после 5 минут становится явно retryable `upload_preparation_interrupted`. Неизменившийся UI poll не пишет в БД. Повтор запускает только доказанные terminal `needs_input|ready_to_retry|failed|cancelled`; `excluded` и `uncertain` никогда не replay-ятся автоматически. Активный `uncertain` держит run в `running`, а вручную остановленная reconciliation (`next_poll_at=NULL`, `manual_uncertain_resolution`) завершает только локальный run, остаётся явно `uncertain_stopped` в summary и также не разрешает retry.
  - `services/marketplace_image_assets.py`, `routes/marketplace_image_assets.py` — обязательная pre-write доставка фото для create/full-state update Ozon. Source URL в fact pack подтверждает только наблюдённый адрес, но не доступность для crawler: queued operation до любого Ozon read/write bounded-проходами заменяет максимум `OZON_MEDIA_ASSET_URLS_PER_ATTEMPT` URL (default 3, range 1..10) за максимум `OZON_MEDIA_ASSET_ATTEMPT_SECONDS` (default 40, range 10..55 секунд) на immutable Seller Hub JPEG. Для create это вся галерея; для update — только exact slots текущего local draft/source overlay. Уже наблюдённая Ozon-CDN галерея из fresh full-state baseline не rehost-ится, иначе URL-only drift ломал бы `already_current` и создавал вечные no-op updates. Частичный payload и новый exact fingerprint атомарно сохраняются в том же operation snapshot с `attempt_count=0`, поэтому restart продолжает оставшиеся слоты и ни один provider вызов не начинается до `media_asset_state=ready`. SSRF-safe Image Lab transport сохраняет cookies между HTTP/meta-refresh переходами, каждый переход повторно валидируется; ответ обязан декодироваться как изображение не меньше 300×300, source ограничен 20 MiB/50 MP, нормализованный JPEG — 12 MiB. Exact byte-дубли удаляются с сохранением первого порядка. Файл хранится content-addressed по SHA-256 в `MARKETPLACE_IMAGE_ASSET_DIR` (production `/app/data/marketplace_image_assets`), URL `/marketplace-assets/images/<sha256>.jpg?sig=...` подписан HMAC и отдаёт только уже существующие bytes с повторной hash-проверкой, никогда не принимает remote URL и не делает network fetch. Ссылка постоянная для асинхронного Ozon import; cache route не возвращает placeholder. Global `after_request` сохраняет выставленный route-ом `public, immutable` только для успешного signed asset response; 403/404 и обычные UI/API-ответы остаются `no-store`. Временный source failure повторяется не более трёх scheduler-проходов, permanent HTML/SSRF/invalid/small/corrupt/config failure завершает только эту карточку с точным `media_*` code и нулём write-attempts. Run/UI различает `preparing_media`, показывает подготовленные/все фото и не называет syntactic source URL готовой доставкой. Synthetic publication tests могут обходить сеть только при Flask `TESTING=true`; `MARKETPLACE_IMAGE_ASSETS_TEST_ENFORCE=true` включает реальный контракт в integration tests.
  - Перед mass-upload один bounded local-only exact source-link preflight сверяет выбранные карточки с уже наблюдённым каталогом Ozon. Связанный listing задаёт авторитетный opaque `offer_id`: canonical/WB `id-7725-1366` использует существующий Ozon `id-7725-1364` и создаёт `product_update`, а не дубль. Тот же preflight по умолчанию выполняется перед одиночным create draft. Если exact source-кандидат ambiguous либо был явно отвязан продавцом, item получает `existing_ozon_listing_link_unresolved`; create с новым seller suffix запрещён. Старый draft без active operation безопасно выравнивает `published_listing_id`, `offer_id` и отсутствующий exact product type по linked listing; конфликтующие listing/type или active write fail-closed. «Мои товары» одним bounded bulk read показывает linked listing как «На Ozon» даже до появления draft и сразу подписывает действие «Обновить Ozon»; draft-map обязательно содержит `published_listing_id`.
  - Deterministic category preflight переиспользует Ozon type только для seller-scoped exact WB-subject/supplier-category identity, когда минимум две разные canonical карточки в текущих exact-linked observations единогласно показывают один fresh official type и нет ни одного stale/unknown/conflict observation; копии одного товара в двух кабинетах не считаются двумя доказательствами. Для этого типа обязательный Ozon-атрибут `8229` («Тип») получает только exact unique значение свежего type-scoped словаря, нормализованно равное official `MarketplaceProductType.name`. Атрибут `9048` («Название модели») получает observed `vendor_code` только если все текущие listing observations группы имеют fresh (не старше 48 часов) attributes snapshot и доказывают exact normalized equality текущего Ozon model с source vendor code; evidence хранит IDs/counters/hash, но не raw значения. Один mismatch/unknown отключает recipe для всей группы. Цена, ТН ВЭД, признак маркировки и другие compliance/commercial facts по category consensus никогда не выводятся: исторически опубликованный code может быть семантически загрязнён и не становится новым default.
  - Ozon package dimensions берутся прежде всего из явно упаковочных полей observed supplier snapshot. Generic `length_cm/diameter_cm/weight_g` и другие размеры самого изделия не считаются упаковкой. Полный fallback из WB разрешён только через exact same-seller `ImportedProduct.product_id`, fresh `Product.last_sync` не старше 48 часов и `dimensions_json.isValid=true`; четыре положительных `length/width/height/weightBrutto` переносятся одной coherent tuple и никогда не смешиваются с частичным supplier tuple. Stale/invalid/partial WB projection ничего не заполняет.
  - Основной массовый repair выполняется прямо на платформе через `GET /marketplaces/ozon/uploads/<job_uid>/repair` и seller-scoped `POST .../repair/apply`: до 200 строк группируются по той же exact source-category identity, выбранный продавцом official Ozon type применяется к отмеченным строкам группы и опционально сохраняется как manual mapping. В одной форме доступны цена, coherent package tuple mm/g, описание и недостающие simple required attributes; верхняя панель заполняет одно поле сразу во всех выбранных совместимых строках. Пустое bulk-значение копирует первое уже заполненное совместимое значение из выбранных строк: так один exact dictionary choice (например, ТН ВЭД) размножается по группе, но каждая карточка заново разрешает display в своём type-scoped official dictionary и чужой dictionary ID не переносится. `services/ozon_compliance_suggestions.py` может показать до трёх read-only вариантов ТН ВЭД только из fresh exact type dictionary, ранжируя их по official type/category и observed source function/material facts; семантически чужой historical listing consensus не используется. Suggestion никогда не становится значением draft без отдельного клика продавца, максимум маркируется средней уверенностью и объясняет evidence; точная классификация и маркировка остаются ответственностью продавца. Type search и dictionary autocomplete читают только локальный official cache, ограничены текущим run/draft/type/attribute scope и не вызывают Ozon. Перед каждым render и повторно перед submit редактор строит свежую локальную validation из текущей listing projection: сохранённые позиционные пути `attributes[N]` никогда не разрешают очистку после catalog refresh, который не меняет версию draft. Submit bounded до 2 MiB, принимает exact server-rendered row set, unique positive IDs, optimistic `draft_version`, seller/account/imported-product scope и работает под общим account operation lock; ошибка строки сохраняется рядом с ней и не откатывает уже завершённые строки. Сохранение только обновляет/валидирует local drafts и выставляет `ready_to_retry|needs_input|excluded`, не создаёт `MarketplaceOperation`; provider write остаётся отдельной явно подтверждённой кнопкой на итоговом экране. Completeness UI не показывает misleading `supplied/schema_total` как долю качества: отдельно выводятся обязательные, число заполненных и размер всей технической схемы, куда входят необязательные PDF/video/Rich Content/оптовые поля.
  - `services/ozon_bulk_repair.py` дополнительно даёт завершённому run необязательный XLSX round-trip до 200 строк: редактируются те же цена, полный комплект упаковки в mm/g, описание и отсутствующие простые required attributes; dictionary cells используют только fresh exact type-scoped official values. Файл bounded до 2 MiB/20 MiB распакованного содержимого и проверяет ZIP structure, contract/run/seller/account, неизменный набор столбцов, unique exact `draft_id + imported_product_id`, optimistic `draft_version`, отсутствие формул и active account operation под общим account lock. Dictionary display локально резолвится в один exact official ID; partial package tuple и stale dictionary fail-closed. Импорт XLSX только обновляет/валидирует local drafts и выставляет `ready_to_retry|needs_input|excluded`: он не создаёт `MarketplaceOperation`, не вызывает provider и не заменяет platform editor либо отдельное write-confirmation. Запрещённый бренд по умолчанию получает recoverable run-local `ИСКЛЮЧИТЬ`; товар/черновик не удаляются, новый явный запуск может вернуть его после законного исправления source.
  - Full-state update использует свежую (hard TTL 48 часов) exact-account `MarketplaceListing` projection как локальный before-state, но не превращает её в canonical master. Existing Ozon attributes/complex groups/media/physical/commercial/barcode сохраняются как replace-style baseline; проверенный draft перекрывает content, exact attribute identities и непустые physical/commercial values, а source-фото только URL-dedupe добавляются после текущей галереи до общего лимита 30 без удаления live primary/слотов. Непредставимые multiple barcodes, пустой display при dictionary ID, неизвестный type, stale/incomplete snapshot или unsupported media fail-closed дают поштучную причину до operation. Projection fingerprint обязателен в каждой update operation: scheduler независимо реконструирует exact live info+attributes+prices+pictures и при любом отличии завершает `update_before_state_drift` с `attempt_count=0`, не вызывая write.
- `services/ozon_brand_policy.py`: единый статический denylist запрещённых Ozon-брендов для всех create/update потоков. Match exact после Unicode/case/space/punctuation normalization, но никогда не substring/fuzzy (короткие `ON`/`HOT` не блокируют чужие составные бренды). Validation проверяет observed source, текущий `ImportedProduct.brand` и seller-edited exact brand attribute; совпадение даёт `ozon_brand_forbidden`, `needs_input`, `brand_allowed=false` и не создаёт provider operation. Бренд автоматически не удаляется и не заменяется.
- `services/marketplace_nav.py`: Jinja-глобал `mp_nav()` для shell-слоя — включён ли Ozon и активные Ozon-кабинеты текущего seller (только id/label/is_default, один SELECT на запрос с кэшем в `flask.g`; роут может отдать уже загруженные кабинеты через `prime_ozon_accounts_cache`). Источник данных общего channel bar; при выключенном флаге/ошибке отдаёт пустой безопасный ответ. Также «липкий» кабинет: `before_request` на `/marketplaces*` запоминает последний явный `account_id` в session, `mp_nav().last_account_id` отдаёт его только если он среди активных кабинетов seller — ссылки сайдбара на Ozon-разделы сохраняют контекст кабинета между разделами.
- `services/ozon_product_import.py`: whitelist-only контракт `/v3/product/import`, строгая нормализация task status и quota response.
  - Полный observed список штрихкодов остаётся в canonical/source fact pack, но Ozon draft детерминированно выбирает первый валидный source barcode, потому что current `/v3/product/import` принимает один `barcode`. Автоматически переносить несколько ШК в draft и затем блокировать mass-upload запрещено; явно seller-edited список больше одного по-прежнему fail-closed отклоняется payload validator.
- `services/ozon_product_state.py`: ORM-free exact full-state reconstruction из info/attributes/prices/pictures и fresh normalized listing projection, canonical fingerprints и archive contract; multiline attribute values поддерживаются как wire-факт, provider empty optional attribute/complex placeholders не фабрикуются обратно, raw provider body не сохраняется.
- `services/marketplace_publications.py`, `routes/marketplace_operations.py`: durable seller-scoped Ozon create/full-update/rollback operations, snapshots, manual submit, polling/reconciliation и audit UI/API.
- `services/marketplace_auto_publish.py`: deterministic multi-account draft provisioning и account-scoped Ozon auto-publish queue с quota allocation, atomic cancellation boundary, circuit breaker и durable operation reconciliation; этот поток не меняет WB-shaped `ImportedProduct.import_status`.
- `services/marketplace_warehouses.py`: полные warehouse snapshots и точные FBS/rFBS observations по `listing + warehouse`; адреса и контакты не сохраняются.
- `services/marketplace_commercial.py`, `routes/marketplace_commercial.py`: reviewed price/stock proposals, live drift preflight, single-attempt writes, reconciliation и conflict-aware rollback proposals.
- `services/ozon_commercial_contracts.py`: ORM-free whitelist/exact-set контракты current price, warehouse и stock endpoint families.
- `services/ozon_analytics_contracts.py`: ORM-free exact request/response contract для read-only `/v1/analytics/data`; каждая метрика имеет provider name, unit, definition version и явный запрет неявного сравнения с WB.
- `services/marketplace_analytics.py`, `routes/marketplace_insights.py`: durable account-scoped Ozon analytics snapshots, normalized metric facts, last-good reads и UI/API `/marketplaces/analytics`.
- `services/admin_sales_intelligence.py`, `routes/admin_sales_intelligence.py`, `templates/admin_sales_intelligence.html`: admin-only центр бестселлеров `/admin/sales/` по уже синхронизированным локальным WB sales и последним completed Ozon analytics snapshots. Финансовые итоги всегда раздельны по маркетплейсам; общий opportunity rank нормализуется внутри канала. Массовый выбор до 50 строк создаёт только durable `BestsellerImageRecommendation` для seller-facing Фотостудии, без LLM/provider/marketplace calls.
- `services/marketplace_quality.py`: детерминированная Ozon quality projection по fresh type schema, fresh listing snapshot и свежему analytics snapshot; WB `Quality Score v2` не переиспользуется как будто определения совпадают.
- `services/ozon_fulfillment_contracts.py`: ORM-free whitelist-контракты current Ozon postings/returns/cancellation feeds; buyer PII и свободные provider payloads отбрасываются до ORM.
- `services/marketplace_fulfillment.py`, `routes/marketplace_fulfillment.py`: durable account-scoped read-only sync и UI/API `/marketplaces/orders`, `/marketplaces/returns`, `/marketplaces/cancellations`; WB order/finance models не переиспользуются.
- `services/ozon_finance_contracts.py`: ORM-free signed-money/cursor/exact-set contracts для current Ozon accrual by-day/types/postings; top-level fact и nested explanatory fee разделены.
- `services/marketplace_finance.py`, `routes/marketplace_finance.py`: immutable last-good account snapshots и UI/API `/marketplaces/finance`; partial run скрыт, currency/marketplace rollup запрещён.
- `services/ozon_feedback_contracts.py`: ORM-free whitelist-контракты current Ozon reviews v2 и questions v1; status/date/cursor/identity проверяются до ORM, provider links и author fields отбрасываются.
- `services/marketplace_inbox.py`, `routes/marketplace_inbox.py`: durable exact-account read-only inbox `/marketplaces/reviews` и локальные AI/template reply drafts. Provider send в этом контуре отсутствует.
- `services/marketplace_operation_locks.py`: process-shared non-blocking file locks. Ozon account scope сериализует publication/credential mutation/health check/disconnect; WB seller media scope общий для legacy import/enrichment multipart/URL photo writes и gallery replace/reconciliation, а отдельный WB seller content scope сериализует full-card replacements и их live preflight. Поэтому локальные provider writes одного типа не выполняются параллельно на одном shared-filesystem host; media/content scopes разделены, чтобы долгий image I/O не блокировал независимый content lifecycle. Docker entrypoint до drop-privileges fail-fast проверяет, создаёт и передаёт `app:app` с mode `0700` точный `/tmp/seller-hub-marketplace-publication-locks`; root-диагностика не должна первой создавать этот каталог или запускать application services без `docker exec -u app`.
- `scripts/probe_ozon_read_contracts.py`: optional live read-only contract probe для catalog/warehouses, finance accrual types/current-day и capability-proven reviews/questions. Он принимает credentials только из process env или owner-only `/tmp/ozon_live.env`, вызывает исключительно manifest endpoints с `retry_class=read` и выводит только bounded response shapes без scalar values/offer/SKU/customer text/raw errors.
- `services/marketplace_rollout.py`: P11 bounded WB `Product -> MarketplaceListing` projection, DB-leased keyset batches до 200 rows, durable parity sweeps и fail-safe common-read cutover. Этот сервис не вызывает WB/Ozon/LLM.
- `services/marketplace_readiness.py`, `routes/marketplace_readiness.py`, `templates/marketplace_readiness.html`: seller-scoped operational dashboard `/marketplaces/readiness/` с effective flags, projection/parity, sanitized Ozon account/reference/listing/draft/operation/sync aggregates; Client-Id, credential ciphertext и raw payload в документ не входят.
- `scripts/manage_marketplace_rollout.py`: secret-free status/backfill/parity/pause/resume CLI. Production rollout, outage и restore порядок зафиксированы в `docs/OZON_PRODUCTION_RUNBOOK.md`.
- `templates/`: Jinja2 UI. Общая оболочка и design tokens находятся в `templates/base.html`.
- `static/`: CSS/JS без отдельного frontend build. TailwindCSS и Alpine.js подключены через CDN.
- `migrations/`: идемпотентные SQLite migration scripts. Это не Alembic.
- `scripts/`: init, backup, diagnostics и operational utilities.
- `tests/`: смешанный набор pytest-style и `unittest.TestCase` тестов.
- `docs/`: дополнительная документация. При расхождении команд доверяйте текущим Compose/entrypoint и этому файлу.

### Marketplace-neutral foundation и Ozon

- Полный scope, parity matrix и волны P0–P12 зафиксированы в `docs/OZON_MARKETPLACE_IMPLEMENTATION_PLAN.md`.
- `SellerMarketplaceAccount` является operational account текущего seller: Ozon хранит non-secret `Client-Id` отдельно от Fernet-encrypted API key. Один seller может иметь до 10 кабинетов одного маркетплейса; default выбирается только внутри `seller + marketplace`. Seller явно один раз выбирает `settings_json.default_vat` из поддерживаемого Ozon enum; public serializer отдаёт только этот allowlisted preference. Новые/старые drafts получают `currency_code=RUB` и отсутствующий account VAT автоматически, но заданные на карточке commercial-поля никогда не перезаписываются.
- Новый credential path fail-closed требует валидный `ENCRYPTION_KEY`; fallback на plaintext, сохранённый для legacy WB колонок, здесь запрещён. Секрет не входит в `repr`, JSON/HTML, status/error или logs.
- `MarketplaceRegistry` явно регистрирует `LegacyWildberriesAdapter` и `OzonAdapter`. Endpoint versions хранятся per capability; Ozon transport не принимает произвольный URL/path.
- Ozon read-only POST может bounded-retry transport/429/5xx. Ozon write POST автоматически не повторяется после transport/5xx/malformed success: durable operation переходит в `uncertain` и сначала сверяется по task/offer live state.
- Актуальный Ozon manifest использует description-category v1, product list/info v3, product attributes v4, pictures read v2/write v1, product import v3 + status v1, archive/unarchive v1, limits v4, prices read v5/update v1, aggregate stocks read v4, per-warehouse FBS read v2, per-warehouse FBO read v1, stocks update v2, warehouses v2, analytics data v1, postings FBS v4/FBO v3, returns v1/rFBS v2, conditional cancellation v2, finance accrual by-day/types/postings v1, reviews list v2 и questions list v1. Deprecated category/product endpoints, postings FBS v3/FBO v2, conditional cancellation v1, per-warehouse FBS v1, warehouse v1 и устаревающие finance transaction v3 запрещены; старый review list v1 также не является fallback. По уведомлению Ozon от 14.07.2026 finance v3 отключат 08.09.2026; не добавляйте временный fallback. В `/v3/product/import` `offer_id` обязателен, а `images360` удалён 10.07.2026.
- `services/ozon_commercial_contracts.py` является ORM-free fail-closed boundary для P6: price/stock builders принимают только whitelist-поля и exact identities, response обязан быть exact-set без чужих/повторных/пропущенных результатов, stock всегда содержит точный `warehouse_id`, а warehouse/FBS pages требуют корректную cursor pagination. Platform batch cap равен 100 даже там, где upstream допускает больше. Сам contract layer не даёт права на provider write; разрешённый side-effect path находится только в `MarketplaceCommercialService` после durable proposal, отдельного human approval и повторного live preflight.
- `scripts/probe_ozon_read_contracts.py` не имеет write mode, загружает live credentials только из process env или owner-only файла и выводит только bounded shapes. Из `/v1/roles` он сохраняет только фиксированные boolean-проверки известных методов, не role names и не произвольные method values. Не расширяйте probe endpoint-ом, который не помечен `retry_class=read`.
- Seller account/catalog/draft/commercial/quality/analytics/fulfillment/finance UI и live Ozon checks включаются через `MARKETPLACE_OZON_ENABLED=1`; orders/returns/cancellations/finance sync всегда read-only и не требует write flag. Новый manual product write требует также `MARKETPLACE_OZON_PUBLICATION_ENABLED=1`, Ozon auto-publish дополнительно требует отдельный `MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=1`, а approve price/stock proposal — независимо `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=1`. General Ozon и manual publication теперь default-on (`1`) в application/Compose/example env для production rollout; emergency rollback остаётся явным значением `0`. Auto-publish и commercial writes остаются default-dark (`0`) и требуют отдельного включения. Все flags явно передаются в web container через Compose. Выключение write flag запрещает только новый side effect и отправку queued rows; scheduler продолжает submitted/polling/uncertain reconciliation. Выключенный general Ozon flag блокирует create/apply canonical-content diff, но не safety-действия reject/rollback существующего diff и disconnect account. P11 local flags независимы: `MARKETPLACE_WB_PROJECTION_ENABLED=1` и `MARKETPLACE_WB_DUAL_READ_ENABLED=1` default-on, `MARKETPLACE_WB_COMMON_READ_ENABLED=0` default-dark. Даже при requested common read `/products` использует `MarketplaceListing` membership только после covering completed backfill/parity без missing/mismatch и после последнего изменения Product; иначе автоматически остаётся legacy `Product` query. Зелёный common read дополнительно содержит SQL-level missing-projection gate: concurrent новая/удалённая projection переключает конкретное выполнение на полную legacy membership и не скрывает карточку.
- Ozon reference truth хранится отдельно от WB-shaped `MarketplaceCategory`: `MarketplaceTaxonomyCategory`, `MarketplaceProductType`, `MarketplaceAttributeDefinition` и `MarketplaceAttributeValue`. Идентичность типа всегда `description_category_id + type_id`; value ID никогда не переносится между attribute/type scopes.
- Global Ozon taxonomy использует только явно настроенный `MarketplaceReferenceAccount`, никогда случайный seller key. Tree обновляется каждые 24 часа, stale enabled schemas bounded-пакетом каждые 6 часов; required dictionaries синхронизируются eager в общем dictionary budget. Все scope jobs используют non-blocking file claims.
- Последний полный Ozon snapshot остаётся structured truth до hard TTL 48 часов. Empty/malformed/duplicate/partial/anomalously shrunk ответ не меняет reference rows. Dictionary checkpoint наблюдаемый и не является resume cursor: без staging retry обязан начать с нуля. Display-only prose в `attribute.description` и `value.info` может содержать provider CR/LF/tab: они нормализуются в один пробел, но прочие control characters по-прежнему fail-closed отклоняют snapshot; structural ID/name/type остаются строгими. Admin restriction может быть только exact subset fresh official dictionary; required attribute нельзя отключить.
- `MarketplaceListing` является общей published read projection, но не master product: Ozon row всегда содержит seller/account/marketplace scope и раздельные opaque `offer_id`, `external_product_id` и SKU; WB row является временным backfill с `legacy_product_id` и nullable account. Ozon никогда не получает fake `Product.nm_id`.
- Startup listing migration переносит не больше 200 отсутствующих WB rows. Остаток и последующие repair sweeps принадлежат `MarketplaceProjectionRun`: stable target watermark, `Product.id` keyset cursor, короткая SQLite lease и atomic listing+cursor commit. Scheduler каждую минуту выбирает до трёх sellers по oldest activity; один service/API/CLI вызов не принимает batch больше 200. Parity history является durable comparison metric; sample хранит только IDs и имена полей. Неправильная non-null canonical link не перетирается автоматически и блокирует cutover.
- `MarketplaceCatalogSync` хранит durable phase/cursor/total/counters. Ozon sweep идёт по `ALL`, затем `ARCHIVED`; страница list + product info + attributes + prices + stocks полностью валидируется до одного commit. Pause/failure сохраняет cursor и последний read model, но не снимает availability. Missing rows помечаются только finalizer-ом после полного прохода обеих фаз; force restart начинает новый run с первой страницы.
- Current product info v3 `statuses.status_failed` — bounded string failed-stage (`""` означает отсутствие, непустое значение переводит listing в `error`), а не boolean. `is_created` остаётся strict boolean. В том же response `primary_image` — strict list из нуля или одного URL; normalized read model хранит один scalar `media.primary_image`. Top-level `sku=0` означает ещё не назначенный SKU и не сохраняется в identifiers; положительный integer остаётся строгим opaque ID, отрицательные/boolean значения отклоняются. Loose coercion string/list или string/bool между этими полями запрещён.
- Current product attributes v4 может возвращать наблюдённый длинный текст характеристики (включая описание) больше 5 000 символов; catalog read принимает его целиком только до строгого per-value cap 10 000 символов и общего snapshot cap 256 KiB. Пробельный optional value нормализуется как существующий empty sentinel `None`, но non-string не приводится к строке. Обрезание, unbounded storage и выдача partial page за успешную запрещены.
- Пустой complex-attribute container из current v4 (`{}` или `attributes=null`) нормализуется только в `{attributes: []}`. Non-object container и любое непустое `attributes` не-list значение остаются protocol error.
- Seller catalog UI/API находится на `/marketplaces/listings/`, фильтруется только внутри `current_user.seller`, а внешний read запускается только для составного `seller_id + account_id` после connected/active/expiry checks. Один account сериализуется DB running-index и non-blocking file claim; один HTTP запуск ограничен 50 list pages, UI использует bounded 5-page пакет.
- `MarketplaceProductDraft` является отдельной seller/account-specific проекцией `ImportedProduct`, а не HTTP body и не `Product`. `MarketplaceCategoryMapping` уникален внутри seller + marketplace + exact source identity; автоматически применяется только `active` mapping на доступный enabled type. Exact `Product.subject_id` связанной WB projection имеет приоритет; `ImportedProduct.wb_subject_id` допускается только после подтверждённой WB projection (`Product`/`product_id`/`wb_nm_id`/`import_status=imported`). Тогда identity `wb_subject:<id>` сильнее строки категории поставщика и сохраняется seller-wide с `supplier_id=NULL`; неподтверждённый AI/pre-publication `wb_subject_id` не расширяет mapping. Если WB mapping отсутствует, legacy supplier/source category остаётся fallback. Title/category fuzzy match и перенос WB dictionary IDs запрещены. При включённой публикации seller-facing «Загрузить на Ozon» создаёт/переиспользует draft, применяет только отсутствующие account defaults, запускает deterministic validation и ставит готовую карточку в durable очередь; локальная «Подготовить Ozon» остаётся compatibility fallback при выключенном write flag. `mapping_readiness` отдельно показывает актуальность canonical fact snapshot, exact category mapping, schema hash/version, обязательные атрибуты и official dictionaries; stored `ready` не перекрывает ставший stale reference/source/account state. `suggest_product_types` — детерминированный лексический подбор кандидатов типа (по имени WB-предмета и категории поставщика среди enabled/available типов) для черновика без типа: только предложение на detail-странице, привязка остаётся существующей явной кнопкой с optimistic version, auto-apply отсутствует. Страница черновика показывает человекочитаемые проекции блоков и снимок фактов с trust-бейджами provenance; raw-JSON редактирование сохранено в раскрывашках с неизменным контрактом update.
- Обратная сверка Ozon → `ImportedProduct` создаёт durable `MarketplaceCanonicalContentProposal` только из fresh (не старше 48 часов) полного `info + attributes` observation одного active seller-owned listing. Разрешены только `title/description`; category/type/attribute/dictionary IDs, brand без exact semantic mapping, цены, остатки, media, dimensions, barcodes и fulfillment исключены контрактом. Create/apply/reject используют общий account operation lock со сменой credentials/disconnect. Apply требует human confirmation и `expected_version`, повторно проверяет source/canonical fingerprints, выполняет conditional SQL update по exact baseline и пишет `AgentChangeSnapshot`; он меняет только master, не вызывает WB/Ozon API. Disconnect переводит pending proposal в conflict, но applied history и локальный rollback сохраняются. Rollback тем же atomic contract возвращает exact baseline только пока нет более нового canonical edit, иначе сохраняет conflict.
- Unified chat принимает Ozon selection только как `entity_kind=marketplace_listing` с exact integer `ids`, `marketplace_code`, `account_id` и `scope_mode=selected`. Browser scope считается недоверенным: harness повторно ground-ит полный набор через `seller + marketplace + account`, task хранит listing IDs отдельно от `product_ids`, а internal brief требует exact match с assigned task. Listing IDs никогда не передаются в WB/ImportedProduct skills. `marketplace-listing-audit` читает только локальный snapshot без LLM, `marketplace-listing-insight` делает не более одного bounded model call; оба имеют пустой tool allowlist и не расшифровывают credentials. Marketplace write из этого scope не попадает в legacy content writer и требует отдельного proposal contract.
- Image Lab принимает optional Ozon target только полным typed envelope `entity_kind + listing_id + marketplace_code + account_id`, повторно ground-ит его через seller и связанную canonical-карточку до создания job и ещё раз после atomic claim до provider work. Результат остаётся локальным `900×1200`, помечается review-only и никогда автоматически не прикрепляется/публикуется: сначала human review и public hosting URL. Наблюдённые Ozon CDN URL не передаются как master input и не раскрываются в target context.
- Content Factory сохраняет durable `catalog_source=legacy_wb|marketplace_listing`; Ozon-фабрика всегда привязана ровно к одному active seller-owned account и хранит карточки как typed `entity_refs_json`, никогда как перегруженные `Product` IDs. Общий текст/AI-кэш/фото берутся из canonical `ImportedProduct`, а цена/остаток — только из локального exact-account Ozon snapshot. Inactive/archived/unlinked/out-of-stock listing завершается до AI-client creation; публичный Ozon URL из SKU/product ID не фабрикуется. После первого `ContentItem` scope фабрики неизменяем, для другого кабинета создаётся отдельная фабрика.
- `services/content_auto_publisher.py`, `services/social_account_publish_health.py` и `services/content_publishers/`: автоматическая публикация использует typed `PublishResult.error_code + terminal`. Терминальные VK auth/capability коды `5/7/15/27` после первой ошибки ставят durable quarantine на точный `SocialAccount` и не расходуют следующие approved items; transient transport/photo ошибки аккаунт не блокируют. Quarantine относится только к scheduler: ручная публикация остаётся явной перепроверкой и снимает блокировку только после успеха. Ручной и автоматический flow делают durable `publishing` claim с exact `social_account_id` только после локального account/platform preflight. Singleton scheduler bounded-пакетом переводит claim старше 30 минут в `failed` с outcome-unknown сообщением, не вызывает provider повторно и требует проверить соцсеть перед явной ручной попыткой. Для VK `access_token` сообщества используется в `wall.post`, а отдельный `user_token` — в photo upload; токены, их prefixes и upload URL не возвращаются диагностикой и не логируются. `migrate_add_social_account_publish_health.py` добавляет typed health fields без backfill legacy ошибок.
- Draft UI/API `/marketplaces/drafts/` использует optimistic `expected_version`; draft/account/imported source проверяются составно с seller. Выключенный feature flag блокирует draft writes, но оставляет существующее состояние read-only. WB/Ozon никогда не образуют механический round-trip: Ozon category/type, attribute ID и dictionary value ID остаются в Ozon projection. Реализованный Ozon → canonical перенос является reviewed diff общих `title/description`; WB и Ozon после него проходят отдельную channel validation/publication.
- Ручной create остаётся create-only: existing offer в `ALL|ARCHIVED` блокирует его до write. Связанный published draft редактируется как новая optimistic revision и отправляется отдельным `product_update`: сервис восстанавливает полный current state из четырёх exact reads, фиксирует prior payload, использует update quota и считает success только после полного live fingerprint match.
- `MarketplaceOperation` хранит durable lifecycle и bounded sanitized results, а `MarketplaceListingSnapshot` — exact submitted/confirmed state. Raw provider response, idempotency key, credentials и submitted payload не возвращаются public routes. Один account write/reconcile сериализуется file claim; scheduler обрабатывает bounded due batch каждую минуту.
- P6 разделяет `MarketplaceCommercialProposal` (human review) и `MarketplaceOperation` (provider side effect). Proposal creation делает только live read; approve повторяет live read и при drift завершает без write. Snapshot и `attempt_count=1` commit-ятся до единственного provider write, после чего разрешены только read-after-write reconciliation; malformed/ambiguous результат не ретраится. Rollback также является новым proposal и создаётся лишь когда live state точно равен original submitted state. `MarketplaceWarehouse` не хранит адреса/телефоны, `MarketplaceWarehouseStock` хранит только точный seller/account/listing/warehouse FBS observation; aggregate stock никогда не является write baseline. Миграция `migrate_add_marketplace_commercial.py` сохраняет старые P5a rows при расширении operation/snapshot CHECK contracts и fail-fast подключена после operation migration.
- Product rollback всегда является отдельным подтверждённым write. Для create он вызывает `/v1/product/archive` ровно для созданного `product_id`, только если full live state не дрейфовал; для update восстанавливает точный prior full payload тем же async import contract. Оба пути commit-ят отдельную operation до write, не ретраят ambiguous response и обновляют parent snapshot. Beta visibility не является archive и не используется.
- `uncertain` можно вручную остановить только через audited `stop_reconciliation_release_local_quota`: outcome остаётся `uncertain`, write не повторяется, credentials продолжают блокироваться. Нельзя превращать эту кнопку в ручное неподтверждённое `succeeded`.
- Изменение Client-Id/API key, connection recheck и disconnect используют тот же account lock. Credential mutation блокируется при любой active operation; disconnect отменяет только `queued` с `attempt_count=0`, но сохраняет ключ для submitted/polling/uncertain и любого write с ненулевой попыткой.
- Legacy WB auto-publish является scope `marketplace_code=wb, account_id=NULL`. Ozon settings/run/item всегда привязаны к одному exact seller-owned account и имеют независимые lock, daily counter, retry history и circuit breaker. Supplier import создаёт локальный draft для каждого enabled Ozon target без LLM/provider вызова; pause блокирует provider writes, но не deterministic draft preparation.
- Ozon auto-publish до каждого нового write атомарно claim-ит item только пока exact run остаётся `running`, а settings enabled и не paused. Cancel/pause/disable, зафиксированный первым, делает provider call невозможным; если submit boundary уже пересечена, run остаётся `cancelling` до operation reconciliation и не выдаётся за отменённый upstream write. После restart item без boundary откладывается, а committed idempotency связывается с durable operation до любого безопасного продолжения.
- File claim координирует процессы только на общем host/filesystem. Текущий Compose имеет singleton scheduler и один web container; перед multi-host/web-replica rollout P11 обязан заменить claim распределённой блокировкой либо гарантировать shared lock filesystem.

### Массовое обогащение общего каталога поставщика

- Администратор запускает обработку из `/admin/suppliers/<supplier_id>/catalog-enrichment` по exact selected IDs либо по текущему server-side фильтру. Целью является одна общая `SupplierProduct`, а не seller-owned `ImportedProduct`, WB `Product` или marketplace listing: результат один раз сохраняется в каталоге поставщика и не вызывает WB/Ozon API. Route повторно проверяет admin role и принадлежность каждого ID точному supplier; одна active run на supplier защищена partial unique index и non-blocking file claim.
- Категорийный preflight fail-closed требует активный WB marketplace, `categories_sync_status=success`, snapshot не старше 48 часов и хотя бы одну `is_leaf + is_enabled + is_available` категорию. Существующий валидный `wb_subject_id` и точное имя leaf-категории источника канонизируются детерминированно без LLM. Для остальных Python выдаёт модели только bounded candidates из этого snapshot; модель не имеет write tools и не может вернуть чужой `subject_id`. Один category chunk содержит до 20 товаров и допускает максимум два model call: первичный выбор и один Python-owned read-only lookup по `lookup_query`.
- Автоприменение model-result требует одновременно confidence не ниже `0.92`, точную source-evidence, полный лексический match предмета не ниже `0.90` и отсутствие равного/более сильного кандидата. Confidence модели сам по себе не является доказательством. Неуверенный, неоднозначный, missing/foreign/duplicate result остаётся `needs_review` без изменения карточки; admin может выбрать только текущий fresh leaf subject. `parent_name` никогда не записывается как предмет. Смена exact subject очищает category-specific AI fields/validation до нового прохода.
- Режим характеристик использует только fresh schema того же exact subject и effective WB dictionaries. Chunk — до 6 товаров и один model call; output обязан быть exact-set по IDs и exact-name subset текущей schema. Каждое сохранённое scalar/list value должно дословно присутствовать в bounded source/evidence и затем пройти общий `MarketplaceAwareParsingTask`/`MarketplaceValidator`; guessing, fuzzy dictionary apply и перенос значения между category scopes запрещены. Новая schema без подтверждённого факта не создаёт характеристику.
- Один запуск принимает максимум 10 000 товаров для `category_only` или 5 000 для `category_and_characteristics`. На item разрешено до трёх попыток, run хранит и атомарно резервирует не более 1 600 model calls; исчерпанный budget честно завершает необработанный хвост ошибкой без write. Детерминированно подтверждённые строки commit-ятся отдельно до fallible model batch. Между category и characteristics сохраняется exact phase checkpoint: если category уже применена, а следующий этап отменён или исчерпал попытки, item остаётся `applied` с исходным `after` snapshot и доступным rollback, а concurrent edit даёт conflict и никогда не поглощается снимком запуска. Run/items, source fingerprint, before/after/reference snapshots, confidence/evidence и counters durable; source/card/reference drift блокирует auto-apply. Review/rollback старого run запрещены, пока для того же supplier активен другой run; rollback разрешён только при exact current `after` state, иначе фиксируется conflict.
- Третий режим `characteristics_inference` — «предположения»: модель видит schema, source и уже заполненные характеристики и предлагает значения ТОЛЬКО для незаполненных словарных полей (значение строго из official/effective словаря; физические величины и free-text не предлагаются). Предложения сохраняются в `SupplierCatalogEnrichmentItem.inference_json` со статусом `needs_review` и НИКОГДА не применяются автоматически, независимо от confidence. Применение — отдельный admin-only endpoint `apply-inference`: повторная канонизация по свежему словарю, source-drift gate, запись в `ai_marketplace_json` с evidence `inference: approved by admin`, snapshot и обычный rollback. Items создаются сразу в phase `characteristics`; миграция `migrate_add_enrichment_inference.py` (rebuild CHECK mode + колонка inference_json) подключена fail-fast в entrypoint.
- HTTP create делает один immediate bounded kick; singleton APScheduler каждую минуту возобновляет до двух runs, каждый tick обрабатывает не более трёх batches. `SUPPLIER_ENRICHMENT_LLM_CONCURRENCY` (1..8, default 1) разрешает одному batch характеристик выполнять до N model-вызовов параллельно: содержимое чанка не меняется, каждый вызов резервируется в llm-бюджете по одному, worker-поток делает только HTTP через собственный AIService instance, а все ORM-записи остаются последовательными в главном потоке (SQLite — один писатель). Исчерпание бюджета при резервации fail-closed завершает run; зарезервированный, но не выполненный вызов не уходит в сеть. После restart stale `running` item возвращается в очередь, cancel проверяется между chunks. File claim требует общего filesystem и не заменяет distributed lock для multi-host topology.
- `SupplierProduct.content_revision` увеличивается только при фактическом изменении общей категории/характеристик или conflict-safe rollback. `ImportedProduct.supplier_content_revision` хранит последнюю явно скопированную версию; migration считает существующие копии синхронизированными на момент rollout. В seller-каталоге есть счётчик/фильтр «Только с обновлениями» и явное локальное «Обновить карточки». Этот шаг копирует текущую общую категорию и fact-bound характеристики, включая безопасные удаления после rollback, но не меняет `Product` и не публикует в WB/Ozon. Синхронизация опубликованной карточки остаётся отдельным seller-confirmed channel write с повторной проверкой её live subject/schema.
- Тот же индикатор доступен в «Моих товарах»: отдельная sticky-вкладка «Обновления поставщика · N» (`?updates=1` показывает и опубликованные строки), бейдж на строках и сфокусированное действие «Применить в мои карточки» (`POST /my-products/refresh-from-supplier`, до 200 exact seller-owned ImportedProduct IDs, вызывает тот же `update_seller_products`, WB/Ozon не трогает). На этой вкладке один bounded экран показывает до 200 строк, фильтры и POST→GET сохраняют `updates=1`, обычные status-вкладки снимают его, а нерелевантные WB/Ozon/AI/delete bulk-действия скрыты. После локального обновления баннер-CTA предлагает опубликованным карточкам переход на существующий подтверждаемый экран enrich-bulk с `preset=characteristics` (предвыбраны характеристики+габариты, фото выключены); default экрана без preset остаётся photos-only. Scheduler-job `notify_supplier_updates` раз в 6 часов создаёт уведомление продавцу о накопившихся обновлениях (локальный SQL, дедуп 24 часа по заголовку).

Дополнительные инварианты этого контура:

- Photo comparison receipt policy `wb-enrichment-preserve-v4` фиксирует `comparison_variant=square_preferred`; delayed reconciler совместимо проверяет старые v3-receipts по исходному `tm`-prefix и использует trusted `square` для отправленных source-фото, чтобы смена aspect ratio в WB thumbnail не становилась ложным конфликтом.
- При `preset=characteristics` экран enrich-bulk явно называется отправкой характеристик, раскрывает выбранные характеристики+габариты и оставляет фото выключенными; default без preset остаётся photos-only.

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
  OpenRouter в agent runtime использует только явно переданный `AI_PROXY`
  (HTTP(S)/SOCKS5; URL и credentials не логируются); SOCKS transport входит в
  runtime dependencies. Не направляйте через этот proxy нативный DeepSeek или
  internal API.
- `agents/image_chat_contract.py`: общий для облегчённого agent-образа и web
  контракт chat image flow (`Gemini Flash` prompt-only → OpenRouter
  `google/gemini-3.1-flash-lite-image`). Не
  переносите его в пакет, который `Dockerfile.agents` не копирует.
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

Контентный writer дополнительно связывает `product_id` JSON Schema enum-ом только с внутренними ID текущего чанка: WB-артикулы/nmID из текста пожелания никогда не являются write-ID. Hard-failure structured-валидации до обработки первой карточки возвращается как `failed`; если предыдущий read-шаг уже завершён, весь workflow честно остаётся `partial`, и UI показывает «Частично выполнено», а не зелёное «Завершено».

Раздел «Качество карточек» (`routes/card_quality.py`, `services/card_quality_scorer.py`,
`services/subject_charcs_cache.py`) не вызывает legacy-агентов. Quality Score v2 —
детерминированный: контент относительно конфига категории WB (кэш
`wb_subject_charcs_cache`, TTL 7 дней) плюс метрики воронки продаж, которые парсятся
из того же ответа sales-funnel, что и рейтинги (без дополнительных API-вызовов).
Объективные ошибки WB-индекса в наименовании проверяются общим pure-контрактом
`agents/content_contract.py`: больше 60 символов, точное вхождение значения
`brand`, повтор значимого слова уже со второго раза и allowlisted рекламный
filler считаются `error` и обязаны опустить title sub-score ниже порога
`weak_title`; проверку синонимов и смысловых лишних подробностей выполняет только
reviewed content-writer, а не эвристическая автозамена. Дубликат описания внутри
каталога также имеет статус `error`, а не косметическую рекомендацию.
Причины «требует внимания» хранятся CSV в `Product.attention_reasons`
(коды в `card_quality_scorer.ATTENTION_REASONS`), приоритет — `Product.quality_impact`.
Под-оценка характеристик считается НЕ долей от всех характеристик категории:
категории WB объявляют десятки необязательных полей, заполнить их все нельзя и
не нужно, поэтому старая формула давала под-оценку ниже порога `weak_chars` у
100% каталога (медиана 15 из 100 на проде) и сигнал переставал быть сигналом.
Текущая шкала: 70% веса — доля заполненных обязательных характеристик, 30% —
необязательные с насыщением на `_OPTIONAL_CHARS_TARGET` (10 штук). Подсказка
называет конкретное действие («не заполнено обязательных: N из M»), а не
соотношение с полным списком категории. Порог `weak_chars` при этой шкале снова
различает карточки; не возвращайте деление на `total_w` без пересчёта порогов.
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

### Отзывы и вопросы WB

- `routes/reviews.py`, `services/feedback_service.py`, `templates/reviews.html`
  обслуживают seller-scoped ответы на отзывы и вопросы. Успешный write WB может
  вернуть `204 No Content` или другой пустой `2xx`; это считается успехом и не
  парсится как JSON. Problem JSON нормализуется в bounded публичную ошибку без
  credentials/raw body, transport failure возвращается браузеру JSON с
  конкретным code, а HTML/пустая proxy-ошибка в UI показывается как HTTP status,
  а не как «Неизвестная ошибка» или `JSON.parse`.
- «Ответить на все» параллелит только генерацию черновиков bounded pool из
  четырёх browser-запросов и считает success/failure каждого элемента. Отправка
  готовых ответов в WB остаётся последовательной из-за upstream write rate
  limits; не превращайте её в unbounded `Promise.all`.

### Фотостудия и инфографика

- `routes/image_lab.py`, `templates/image_lab.html`, `static/image-lab.js` —
  seller-scoped лаборатория `/image-lab`: продавец видит до 10 нормализованных
  фото карточки и выбирает режим `single` (один ракурс), `each` (отдельная job
  на каждый выбранный ракурс), `reference_set` (одно главное фото в финале,
  остальные — identity references с ролями `angle|packaging|detail`),
  `collage` (локальный общий макет из 2–10 оригинальных foreground) или
  `angles` (research-only синтез отдельных `front|back|left|right|three_quarter_*|top`
  видов из 1–10 фото одного SKU). Один prompt запускается до трёх раз через
  видимый backend OpenRouter, результаты сравниваются вслепую и оцениваются 1–5
  и тегами. Доступные модели: `google/gemini-3.1-flash-lite-image` (Nano Banana
  2 Lite, 1K, default), `google/gemini-3.1-flash-image` (Nano Banana 2, 2K),
  `x-ai/grok-imagine-image-quality` (Grok Imagine Quality, 2K), а также
  `openai/gpt-image-2` с фиксированным seller-facing профилем `quality=medium`
  и консервативной оценкой `4,50 ₽` за job. GPU, Gen-API и
  AITunnel скрыты из UI и отклоняются для create/repeat; их provider-код и env
  остаются только для чтения/завершения уже сохранённых исторических jobs.
  `ImageGenerationExperiment` хранит воспроизводимые
  параметры, `generation_strategy`, `composition_mode`, главное фото, точные
  indices/roles, `requested_view`, watermark/text configs, стоимость, latency, quality JSON и
  локальные artifacts; миграции — `migrations/migrate_add_image_generation_lab.py`
  `migrations/migrate_add_image_lab_reference_watermark.py` и
  `migrations/migrate_add_image_lab_angle_synthesis.py`. Endpoint preview не отдаёт
  upstream URL в браузер: он tenant-scoped и последовательно пробует cache и
  `sexoptovik/blur/processed/original`, потому что original CDN может быть
  временно недоступен backend-контейнеру.
- Optional Ozon-target Image Lab не меняет source identity: experiment хранит
  exact `marketplace_listing_id + target_context_json`, но использует фото и
  visual context только общей `ImportedProduct`. До provider work target
  revalidate-ится; unlinked/archived/inactive/cross-tenant state завершает job
  без генерации. Target metadata содержит count/fingerprint и ограничения
  `3:4`, `900×1200`, main images `<=30`, no `images360`; локальный artifact не
  считается attachable и всегда требует human review + public hosting.
- `services/image_lab_service.py` владеет allowlist backend/model, prompt
  policy, SSRF-защитой загрузки исходника, seller budgets (active/24h/рубли),
  lifecycle и аналитикой. Browser никогда не получает provider/GPU secrets.
  API берёт seller только из `current_user.seller`; experiment/product/artifact
  всегда выбираются составным `id + seller_id`.
- Admin shortlist бестселлеров является только локальным handoff: исходная строка
  повторно ground-ится по typed `wb:product:<id>` либо
  `ozon:account:<id>:listing:<id>` в текущем server-side срезе, а продавец видит
  только свои `status=recommended` rows в `/image-lab`. Открытие, dismiss и
  completed не вызывают генерацию. Платная job появляется только после обычного
  seller-confirmed запуска с показанной стоимостью и всеми budget/target gates.
  Для exact-linked WB-карточки с пустым `ImportedProduct.photo_urls` ранжирование
  считает bounded слоты из `Product.photos_json`; GET Фотостудии разворачивает
  их в WB CDN URL без записи, а seller-confirmed create копирует этот
  bounded список в canonical `ImportedProduct` в одной транзакции с experiment.
  Fuzzy title/category matching и Ozon media в этом fallback запрещены.
  WB использует нетто-факты `WBSale`, Ozon — `ordered_*` последнего completed
  account snapshot; эти определения и рублёвые суммы нельзя складывать между
  каналами. Миграция —
  `migrations/migrate_add_bestseller_image_recommendations.py`.
- Массовая инфографика запускается из выбранных строк «Моих товаров» и хранится
  отдельно в `InfographicCampaign -> InfographicCampaignItem -> InfographicCampaignSlide`.
  Один запуск принимает не более 200 уникальных positive integer ID одного seller;
  ошибка или нехватка фактов одного товара не блокирует остальные. Текст слайдов
  детерминированно строится только из сохранённого fact pack, исходный товар
  композится локально, а готовые `900×1200` artifacts остаются review-only.
  Новый content имеет `policy=fact_safe_v2`: hero показывает exact title/category/brand,
  остальные слайды содержат 1–4 адаптивные fact cards; `Бренд`
  не дублирует brand hero, а `Материал`/`Состав` с точно тем же
  значением не размножают слайды. Existing v1 durable content остаётся
  рендериться как single fact card. Исходные байты получаются через
  `fetch_original_product_bytes`, поэтому все candidate URL одного слота и тот же
  supplier cache пробуются до явного item failure.
  Пакетное approve/reject является локальным решением редактора и не означает
  публикацию. Миграция — `migrations/migrate_add_infographic_campaigns.py`.
- Публикация одобренных слайдов является отдельным подтверждаемым контуром
  `MarketplaceMediaPublication -> MarketplaceMediaOperation -> MarketplaceMediaOperationSlide`.
  Один preview принимает не более 200 exact campaign items. Для WB target —
  seller-owned `Product/nm_id`; для Ozon — exact seller-owned `account + listing`.
  Политика `prepend` ставит approved slides перед текущими фото, сохраняет хвост
  в исходном порядке и явно показывает отброшенные позиции при лимите 30.
  Перед confirm UI показывает весь финальный порядок, а backend требует
  `expected_version`, отдельное human confirmation и публичный HTTPS
  `PUBLIC_BASE_URL`. Все исходные WB bytes до write сохраняются в private cache;
  карточка с video блокируется, потому что безопасно восстановить video через
  gallery replace нельзя. Provider write имеет ровно одну попытку после durable
  boundary, HTTP success не считается применением без ordered live
  reconciliation; ambiguous result не ретраится вслепую. Rollback — отдельная
  подтверждённая операция с live drift preflight и тем же reconciliation.
  Подписанные media URL живут не более 24 часов и не раскрывают local path или
  seller identity. Ozon preview/readiness уже типизированы, но direct pictures
  write запрещён до встраивания в существующую full-state operation/snapshot
  модель. До этого Ozon media-preview сохраняет и возвращает только bounded
  `main_image_count + main_image_fingerprint`, но не provider CDN URL.
  Миграция — `migrations/migrate_add_marketplace_media_publications.py`.
- Единый чат запускает одну платную генерацию через write-skill
  `image-generator` только для ровно одной подтверждённой typed-карточки.
  План до запуска показывает оценку около `3,30 ₽`, `Gemini Flash → Nano Banana
  2 Lite`, вертикальный `1K` edit, `native_scene` и отсутствие автопубликации.
  OpenRouter тарифицирует точный request в USD, поэтому оценка не является
  фиксированной ценой; account credits проверяются до Gemini и до создания
  experiment. Internal API tenant-scoped
  сопоставляет `Product` только с принадлежащим тому же seller точно связанным
  `ImportedProduct`. Если у этой import-строки нет `photo_urls`, но опубликованный
  `Product.photos_json` содержит WB-фото, brief без записи разворачивает их в
  публичные CDN URL, а create атомарно сохраняет этот exact-link media backfill
  вместе с experiment и checkpoint; похожие товары и fuzzy matching запрещены.
  При отсутствии пригодного исходника чат возвращает конкретное clarification и
  явно сообщает, что платная генерация не запускалась. Create/poll доступны
  исключительно активному подтверждённому `AgentTask`, содержащему этот skill;
  checkpoint не допускает повторного платного POST при retry. Prompt-writer
  делает не более одного bounded structured Gemini Flash вызова только через
  OpenRouter с `AI_PROXY`; AITunnel/native Gemini fallback в этом flow запрещён.
  Необязательная публичная HTTPS-ссылка
  передаётся мультимодально только Gemini как визуальный style reference:
  разрешены композиция, свет, палитра и распределение масс, но чужое фото никогда
  не передаётся в image provider, его товар/упаковка/надписи не копируются.
  В OpenRouter image model уходит только главное фото текущей карточки и
  проверенное описание сцены; финал возвращается `image_generation` artifact с
  3:4-превью в полном и popup-чате, всегда `review_required` и
  `publishable=false`. Числовые ID внутри HTTPS reference URL не участвуют в
  grounding артикула. Фраза «для любой карточки/любого товара» является явным
  разрешением детерминированно выбрать ровно одну seller-owned карточку со
  связанным фото только для approval-плана; выбор отбрасывает очевидно
  несовместимые с image-provider safety категории и предпочитает нейтральный
  упакованный товар, выбранный ID показывается до оплаты,
  а отсутствие такого явного разрешения по-прежнему требует typed selection.
- UI default `native_scene` повторяет удачный pilot mode A: исходное главное
  фото уходит напрямую в image endpoint без mask и
  без включённого по умолчанию текстового product context; модель должна собрать
  единый кадр с общим светом, контактной тенью и отражениями, а не подложить сцену
  под cutout. Безопасный `background_only` остаётся явным режимом: модель получает
  только описание пустой сцены, после чего оригинальный RGB товара накладывается
  локально ровно один раз. Новый OpenRouter flow не предлагает
  `reference_guided`: dedicated image API не принимает protection mask.
  Исторические masked jobs остаются читаемыми, но create/repeat отклоняются.
  `native_scene` передаёт байты исходного главного фото без mask; provider output
  используется напрямую без второго foreground-слоя, всегда имеет
  `identity_mode=generative_edit`,
  `publishable=false` и требует human identity/duplicate review. В
  `reference_set` остальные выбранные байты идут отдельными identity references
  в порядке сохранённого manifest; `packaging`/`detail` являются только evidence
  и не должны появляться лишним объектом. `native_scene` недоступен для
  `collage`; `background_only` остаётся единственным режимом GPU bridge. Bounded
  visual context выбирается между ImportedProduct и
  более полным `SupplierProduct.ai_parsed_data_json`, удаляет цены/ID/instructions,
  ограничен размером и сохраняется в prompt. Пользовательский additional prompt
  не может отменить identity/no-duplicate/no-generated-text правила.
- `angle_synthesis` доступен четырём reference-capable OpenRouter моделям:
  главное фото с ролью `angle` передаётся первым, остальные выбранные фото —
  отдельными evidence references с сохранённым manifest; на каждый выбранный
  `requested_view` создаётся самостоятельная job. Этот поток перерисовывает весь
  товар и синтезирует скрытую геометрию, поэтому использует
  `identity_mode=generative_edit`, всегда имеет `publishable=false` и требует
  human identity/geometry review. Никогда не переносите в него гарантию
  original RGB из `background_only` и не разрешайте auto-publish по rating.
- OpenRouter вызывается через `POST /api/v1/images`: локальные PNG/JPEG/WebP
  референсы кодируются как data URL в `input_references[]`, а output читается из
  `b64_json` или URL. Для Nano/Grok запрос задаёт `aspect_ratio=3:4` и
  `resolution=1K|2K`; `openai/gpt-image-2` получает только объявленные discovery
  API параметры `quality=medium` и `background=opaque`, без неподдерживаемых
  `resolution/aspect_ratio/size`, после чего результат локально нормализуется до
  `900×1200`. Nano и GPT Image 2 принимают до 10 выбранных фото в рамках UI
  limit, Grok — до 3; превышение отклоняется локально до платного POST. Оценка
  GPT Image 2 учитывается в рублёвом seller budget консервативно, фактическое
  списание остаётся token-based значением OpenRouter. Transport использует
  `IMAGE_GEN_PROXY`, затем `AI_PROXY`, затем `HTTPS_PROXY`; proxy URL/credentials
  не логируются. Любой provider output нормализуется локально до 900×1200.
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
- Лимиты/секреты: `OPENROUTER_API_KEY`, `OPENROUTER_IMAGE_MODEL`,
  `OPENROUTER_IMAGE_RESOLUTION`, `IMAGE_GEN_PROXY`, `AI_PROXY`; legacy
  completion-only: `GEN_API_KEY`, `AITUNNEL_API_KEY`, `GPU_IMAGE_SERVER_URL`,
  `GPU_IMAGE_SERVER_TOKEN`, `GPU_IMAGE_ALLOW_HTTP`, `GPU_IMAGE_STEPS`,
  `GPU_IMAGE_TRUE_CFG`, `GPU_IMAGE_RUB_PER_GENERATION`,
  `IMAGE_LAB_MAX_ACTIVE_JOBS`,
  `IMAGE_LAB_DAILY_JOB_LIMIT`, `IMAGE_LAB_DAILY_BUDGET_RUB`,
  `IMAGE_LAB_PROVIDER_TIMEOUT`, `IMAGE_LAB_DATA_DIR`, `IMAGE_LAB_INLINE_WORKER`,
  `PUBLIC_BASE_URL`, `MEDIA_PUBLICATION_DATA_DIR`,
  `MEDIA_PUBLICATION_INLINE_WORKER`,
  `INFOGRAPHIC_REMBG_MODEL`, `GEMINI_API_KEY`,
  `GEMINI_MODEL`, `AGENT_IMAGE_PROMPT_MODEL`,
  `AGENT_IMAGE_PROMPT_MAX_TOKENS`, `AGENT_IMAGE_WAIT_SECONDS`; GPU host
  использует `GPU_BRIDGE_TOKEN`.
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
Тот же runner bounded-пакетами обслуживает queued items массовых кампаний.
Каждую минуту он переводит `running` item старше 30 минут в явный
retryable `failed/worker_interrupted`; provider/write автоматически не повторяется.
Он также обрабатывает durable media operations. Независимый singleton scheduler
каждые 15 секунд делает bounded recovery/process/reconciliation, поэтому
reconciliation продолжается и без optional Image Lab worker. File/DB claims
разрешают совместный запуск; stale preflight без write boundary можно вернуть в
queue, а stale `attempt_count=1` переводится только в `uncertain` для live read.

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

Ozon account/catalog/draft/commercial/quality/analytics/fulfillment/finance/inbox UI и ручная публикация включены по умолчанию (`MARKETPLACE_OZON_ENABLED=1`, `MARKETPLACE_OZON_PUBLICATION_ENABLED=1`). Для локального запуска сначала задайте валидный постоянный Fernet `ENCRYPTION_KEY`; Docker entrypoint проверяет этот инвариант через `scripts/validate_runtime_config.py` до миграций и fail-fast останавливает misconfigured Ozon runtime без вывода секрета. Явное значение `0` остаётся emergency rollback; auto-publish и commercial writes по умолчанию выключены. `/api-settings` является общей точкой входа для WB и Ozon: там seller может создать Ozon account, увидеть sanitized status и запустить read-only connection check; расширенное управление тем же account через тот же `MarketplaceAccountService` находится в `/marketplaces/accounts/`. Seller запускает bounded read-only catalog sweep в `/marketplaces/listings`, загружает до 200 карточек единым flow в `/marketplaces/ozon/uploads/`, готовит и исправляет validated drafts в `/marketplaces/drafts/`, синхронизирует склады/создаёт read-only proposals в `/marketplaces/commercial/`, смотрит отдельные Ozon-оценки в `/marketplaces/quality`, read-only аналитику в `/marketplaces/analytics`, заказы в `/marketplaces/orders`, возвраты в `/marketplaces/returns`, отмены в `/marketplaces/cancellations`, immutable accrual snapshots в `/marketplaces/finance` и capability-gated отзывы/вопросы в `/marketplaces/reviews`. Draft validation и quality recompute не вызывают Ozon или LLM; analytics/fulfillment/finance/inbox refresh вызывает только manifest endpoints с retry class `read`. Reply draft может сделать один bounded seller-scoped AI-вызов, но остаётся локальным. Ручные create/full-update/product rollback независимо включаются `MARKETPLACE_OZON_PUBLICATION_ENABLED=1`; auto-publish требует ещё `MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=1` и включения точного account scope на `/auto-publish`; approve price/stock proposal использует `MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=1`. Операции и reconciliation видны в `/marketplaces/operations/`. Никогда не включайте write flags только ради unit tests: они используют synthetic credentials/adapters и не вызывают Ozon.

При том же `MARKETPLACE_OZON_ENABLED=1` Image Lab может пометить локальный
experiment exact Ozon listing-target, а Content Factory — выбрать
`catalog_source=marketplace_listing` и один кабинет. Эти потоки не вызывают
Ozon write: Image Lab не прикрепляет artifact, Content Factory публикует только
в настроенную социальную сеть и не изменяет marketplace listing. Media
publication wizard показывает exact Ozon account/listing readiness и сохраняет
typed blocked preview, но не вызывает pictures endpoint: Ozon media должен пройти
через существующий full-state publication/snapshot/reconciliation contract.

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
python migrations/migrate_add_supplier_catalog_enrichment.py data/seller_platform.db
python migrations/migrate_add_marketplace_commercial.py data/seller_platform.db
python migrations/migrate_add_marketplace_product_updates.py data/seller_platform.db
python migrations/migrate_add_image_generation_lab.py data/seller_platform.db
python migrations/migrate_add_image_lab_reference_watermark.py data/seller_platform.db
python migrations/migrate_add_image_lab_angle_synthesis.py data/seller_platform.db
python migrations/migrate_add_image_lab_marketplace_target.py data/seller_platform.db
python migrations/migrate_add_infographic_campaigns.py data/seller_platform.db
python migrations/migrate_add_marketplace_media_publications.py data/seller_platform.db
python migrations/migrate_add_bestseller_image_recommendations.py data/seller_platform.db
python migrations/migrate_add_content_factory_marketplace_scope.py data/seller_platform.db
python migrations/migrate_add_wb_dictionary_provenance.py data/seller_platform.db
python migrations/migrate_add_brand_category_external_id.py data/seller_platform.db
python migrations/migrate_add_social_account_publish_health.py data/seller_platform.db
python migrations/migrate_add_marketplace_accounts.py data/seller_platform.db
python migrations/migrate_add_ozon_references.py data/seller_platform.db
python migrations/migrate_add_marketplace_listings.py \
  data/seller_platform.db --backfill-limit 200
python migrations/migrate_add_marketplace_product_links.py data/seller_platform.db
python migrations/migrate_add_marketplace_canonical_content.py data/seller_platform.db
python migrations/migrate_add_marketplace_rollout.py data/seller_platform.db
python migrations/migrate_add_marketplace_drafts.py data/seller_platform.db
python migrations/migrate_add_marketplace_operations.py data/seller_platform.db
python migrations/migrate_add_marketplace_auto_publish.py data/seller_platform.db
python migrations/migrate_add_marketplace_quality_analytics.py data/seller_platform.db
python migrations/migrate_add_marketplace_fulfillment.py data/seller_platform.db
python migrations/migrate_add_marketplace_finance.py data/seller_platform.db
python migrations/migrate_add_marketplace_inbox.py data/seller_platform.db
python migrations/migrate_andrey_feed_full_ingest.py data/seller_platform.db
python migrations/migrate_add_enrichment_inference.py data/seller_platform.db
python migrations/migrate_clean_characteristic_dimensions.py data/seller_platform.db
python migrations/migrate_add_wb_card_audit.py data/seller_platform.db
python migrations/migrate_competitor_monitor_v2.py data/seller_platform.db
python migrations/migrate_compact_competitor_snapshots.py data/seller_platform.db
python migrations/run_all_migrations.py data/seller_platform.db
```

Правила schema changes:

- Создавайте новый идемпотентный script в `migrations/`; не полагайтесь только на `db.create_all()` для существующих БД.
- Не переписывайте уже развёрнутую миграцию так, чтобы старые инсталляции получили другое поведение.
- Миграции, добавляющие колонки, которые ORM читает сразу после старта, должны быть fail-fast в `docker-entrypoint.sh`; не скрывайте их ошибку через `|| echo`.
- Scoped migration делает baseline `PRAGMA foreign_key_check` до DDL и после DDL отклоняет каждое новое нарушение, а также любое нарушение, где child или parent относится к её managed tables. Неизменённые legacy-сироты в чужом домене остаются наблюдаемыми для отдельного repair, но не должны ложно блокировать несвязанную миграцию; blanket-проверка всей исторической БД без baseline запрещена.
- `ImportedProduct` — текущая каноническая seller-owned карточка и единственный источник общего контента/AI-кэша. `Product` остаётся WB projection, `MarketplaceListing` — account/channel projection. Ozon catalog sync может auto-link только одно уникальное literal offer/vendor либо одно уникальное parsed source-ID совпадение по закрытым anchored форматам поставщика; title similarity, barcode, transliteration и LLM не создают связь. Отсутствующую canonical-копию разрешено materialize-ить только local reconciliation под seller-scoped process lock и только при уникальной exact тройке connected supplier + WB + Ozon; никакой marketplace write при этом нет. Ambiguous остаётся unlinked до seller confirmation; явный seller unlink автоматика уважает; link/unlink требуют tenant scope, optimistic `link_version` и append-only event. Одна внутренняя карточка не может быть связана с двумя listings одного account.
- `migrate_add_marketplace_canonical_content.py` идемпотентно создаёт durable reverse proposals и partial unique pending scope после product-link prerequisites. Он подключён fail-fast к Docker, comprehensive runner и direct SQLite startup; existing content не backfill-ится и не меняется.
- `migrate_add_marketplace_rollout.py` идемпотентно создаёт только durable projection/parity run journal и partial unique active scope. Он fail-fast подключён после listing/link prerequisites к Docker, comprehensive runner и direct SQLite startup и никогда не сканирует `products`; data movement выполняет bounded runtime job.
- `migrate_add_image_lab_marketplace_target.py` и `migrate_add_content_factory_marketplace_scope.py` идут fail-fast после marketplace listing/account prerequisites. Исторические content factories получают `catalog_source=legacy_wb`, historical items — пустой typed refs array; существующий WB runtime не переключается автоматически.
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
- Ozon canonical-content proposal всегда выбирается по `proposal.id + current_user.seller.id`; сохранённые marketplace/account/listing/imported-product foreign keys повторно сверяются как один scope. Apply/reject/rollback сверяют optimistic version и authenticated user с `Seller.user_id`; browser не передаёт seller/source state.
- `MarketplaceProductDraft`, его `ImportedProduct`, account, marketplace и category mapping обязаны иметь один seller/marketplace scope. `corrected_by_user_id` берётся из authenticated user и повторно сверяется с `Seller.user_id`; body не может назначить автора исправления.
- `MarketplaceOperation` и snapshot всегда выбираются с `operation.id + current_user.seller.id`; их account/draft/listing scope повторно сверяется. Public serializer не отдаёт `idempotency_key`, submitted state или credentials.
- Auto-publish settings/run/item user routes всегда выбирают selector scope из query, затем exact `settings_id + seller_id + account_id`; body не может сменить marketplace/account. WB row обязан иметь `account_id=NULL`, Ozon row — seller-owned Ozon account. Публичный item serializer не отдаёт внутренний idempotency key.
- Ozon analytics sync/fact/quality, fulfillment posting/item/status/return/cancellation, finance snapshot/fact/component и inbox sync/item/draft rows всегда выбираются по полному `seller_id + marketplace_id + account_id`; bare sync/posting/listing/account/item/fact ID недостаточен. `account_id` для insight/fulfillment/finance/inbox routes задаётся только canonical positive query-параметром, duplicate/unknown query и body scope smuggling запрещены. `created_by_user_id` reply draft повторно сверяется с `Seller.user_id`.
- Любой internal agent request обязан пройти agent authentication, task ownership и assignment-to-seller checks.
- Запрос объекта выполняйте составным условием `id + seller_id`; проверка одного ID недостаточна.
- Область сущности всегда типизирована: `/products/<id>` означает `Product`, а страницы импорта/поставщика — `ImportedProduct`; числовой ID без `entity_kind` неоднозначен.
- `marketplace_listing` дополнительно всегда содержит точный `marketplace_code + account_id`; bare listing ID, numeric string, bool/float, duplicate, mixed-account или foreign-seller selection отклоняются целиком до чтения/LLM. Internal marketplace tool обязан сверить тот же exact-set с active assigned task, а ответ строится только из allowlisted локальных facts без raw provider blobs/credentials.
- Image Lab marketplace target и Content Factory entity refs подчиняются тому же typed scope. Durable Image Lab worker повторно проверяет seller/account/canonical link после claim; Content Factory валидирует exact account/set и stock до AI. Нельзя брать Ozon media URL как canonical photo source или переносить listing ID в legacy `product_ids_json`.
- Admin bestseller selection повторно строится из локального server-side sales read model; browser scope key не является правом на foreign seller/product/listing. Durable recommendation всегда содержит exact seller + canonical `ImportedProduct` + channel target, seller review выбирает её по `recommendation.id + current_user.seller.id`. Recommendation create/open/review не вызывает image provider и не резервирует seller budget.
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

### Цены WB: отправка и подтверждение — разные события

- `POST /api/v2/upload/task` ставит цены в асинхронную очередь Wildberries и не
  означает, что цена встала на витрине. Поэтому `routes/safe_prices.py::batch_apply`
  выставляет позициям `status='submitted'`, батчу — `status='submitted'`, и НЕ
  трогает `Product.price`. Прежнее поведение (`applied` сразу после HTTP-ответа
  плюс запись локальной цены) возвращать запрещено: оно выдавало догадку за факт.
- `services/price_reconciliation.py` — единственный контур, который переводит
  позицию в `applied`. Он читает фактические цены товаров (`get_goods_prices`) и
  сравнивает с отправленными: совпало (допуск 1 ₽ на округление WB) →
  `applied` + `wb_status='confirmed'` + `Product.price` = наблюдённая цена
  площадки + запись `PriceHistory`; не совпало и прошло больше 24 часов →
  `failed` с причиной; иначе позиция честно остаётся `submitted`. Отсутствие
  ответа площадки не считается ни успехом, ни отказом.
- Сверка запускается сразу после отправки (одна попытка, ошибка не ломает
  запуск), вручную кнопкой `POST /prices/batch/<id>/reconcile` и фоновым job
  `reconcile_submitted_prices` каждые 5 минут (до 3 продавцов и 3 запусков за
  тик, только чтение WB). Повторных отправок цен сверка не делает.
- Откат (`batch_revert`) обязан разбирать ответ WB по каждому `nmID`: локальная
  цена возвращается только подтверждённым позициям, отклонённые сохраняют
  текущую цену площадки и получают собственный `status='failed'` с причиной.
  Исходный батч помечается `reverted` только при полном откате.
- Неудавшиеся позиции доотправляются через `POST /prices/batch/<id>/retry-failed`:
  создаётся новый `draft`-батч только из `status='failed'`, где цель берётся из
  исходной `new_price`, а точка отсчёта — из текущей `Product.price`. Повторять
  старую дельту вслепую запрещено.

### Массовое обновление фото WB

- `POST /products/enrich-bulk` является экраном подтверждения для выбранных `Product`: один запуск принимает не более 200 уникальных positive integer ID, до создания `EnrichmentJob` требует exact seller-owned set и не допускает параллельный свежий `pending|running` job того же seller; check+insert дополнительно сериализован host-shared seller job lock внутри сервиса, поэтому конкурентные HTTP-входы не обходят route-precheck. Переход из «Моих товаров» сначала детерминированно переводит выбранные seller-owned `ImportedProduct.id` в их exact `product_id`; неопубликованные строки не подмешиваются.
- Seller-facing default этого экрана — только `photos=true` и стратегия обновления существующей галереи; `title/description/characteristics/dimensions/brand` выключены. Дополнительные поля включаются только явно. Photo-only no-op считается `skipped`, а не успешным write.
- `_run_bulk_job` обрабатывает карточки последовательно, изолирует ошибку одной карточки, после каждой строки durable-обновляет `processed/succeeded/failed/skipped/results` и продолжает exact оставшийся набор. Для связанного импорта актуальная `SupplierProduct.photo_urls_json` имеет приоритет над staging-копией `ImportedProduct.photo_urls`; fuzzy matching фото и перенос между seller запрещены.
- Хаб `/supplier-updates` и enrichment bulk всегда используют bounded synchronous cache + multipart `media/file`, как рабочий одиночный поток; `PUBLIC_BASE_URL` не является условием массового обновления. Локальные standard photo pins детерминированно компонуются вокруг свежей supplier gallery с теми же `first/last`, `pin/fill`, `order`, sparse-threshold и лимитом 30. Photo-only путь не вызывает LLM и не меняет текстовые поля без явного выбора.
- Этот legacy enrichment path остаётся отдельным от генеративной инфографики. Одобренные campaign slides публикуются только через `MarketplaceMediaPublication`: preview точного порядка, public-host gate, human confirm, durable single-attempt operation, live reconciliation и отдельный rollback. Нельзя вызывать legacy automatic photo update напрямую из approve/reject кампании.
- Legacy import/enrichment photo upload и `MarketplaceMediaPublication` обязаны брать один `try_wb_seller_media_lock(seller_id)` вокруг полного provider photo-write lifecycle, включая file → URL fallback. Busy lock является безопасным pre-write отказом; не добавляйте обходной upload path без этого lock.

### Характеристики WB из данных поставщика

- Любой supplier/AI characteristic patch перед созданием или обновлением карточки проходит fail-closed проверку по WB category schema из `MarketplaceCategoryCharacteristic` и тому же effective constraint resolver, который видит AI. До строгого билдера оба name-keyed supplier-контейнера — `{name: value}` и `[{name, value}]` — проходят один `partition_supplier_characteristic_input` (`services/marketplace_validator.py`): dimension-образные имена уходят в объект `dimensions` карточки WB (это не характеристики), а НЕобязательное имя, которого нет в актуальной схеме категории, пропускается с явным отчётом `skipped` вместо блокировки всего патча. ID-based, mixed и malformed массивы никогда не фильтруются частично и целиком остаются строгому валидатору; повторы name-keyed массива сохраняются до проверки дублей. Dedicated `materials`/`gender` являются каноническим источником того же supplier-факта и перекрывают raw-дубль по точному `charc_id` ДО единственной итоговой валидации; для `gender` разрешены только явные языковые aliases официальных значений (`для женщин → Женский`, `для мужчин → Мужской` и детские варианты), после чего значение всё равно обязано дословно пройти свежий `kinds`. Строгость значений/словарей/обязательных полей это не ослабляет; при непроверяемой (stale) схеме фильтр ничего не отбрасывает и патч честно отклоняется целиком. Явный non-empty category `dictionary_json` из admin или WB schema является строгим allowlist. `Цвет`, `Пол|Пол товара`, `Страна производства`, `Сезон` и НДС используют официальные глобальные `MarketplaceDirectory` (`colors|kinds|countries|seasons|vat`) и не подменяются старым category-списком. ТНВЭД загружается из официального category-scoped endpoint только с typed `subjectID` и хранит `isKiz` в dictionary snapshot.
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
- Read-only WB card preview не запрашивает size schema, если в source-карточке нет размеров. При наличии размеров busy/unavailable reference отдаёт читаемое preview с readiness-error, а не HTTP 500 и не ложный вывод «категория безразмерная»; provider write по-прежнему fail-closed повторяет строгую проверку.
- Schema endpoint отдаёт для каждой характеристики компактный `constraint` из того же validator-resolver: `source`, `constrained`, `usable`, `count`, не более 40 `values`, `truncated`, `dictionary_source`, `dictionary_synced_at` и `dictionary_version`. Весь global directory JSON в prompt не передаётся. Непроверяемое required-поле делает всю схему `usable=false` до LLM; optional-поле с `constraint.usable=false` пропускается.
- Batch prefetch категорий и схем использует authenticated `/internal/v1/categories/search-batch` и `/internal/v1/categories/characteristics-batch` с лимитом 1..200: один request делает bounded bulk SELECT, сохраняет порядок входа и возвращает typed fail-closed item для каждого query/`subjectID`, включая missing/stale/unavailable. Поиск в больших allowlists использует `/internal/v1/categories/characteristic-values/search-batch` с 1..50 уникальными typed queries и точным сохранением порядка.
- Batch write-path переиспользует request-level schema/directory validation cache. Не выполняйте повторный набор reference SELECT для каждой карточки одной категории.
- Scheduler и ручные category/directory/schema refresh используют общие non-blocking cross-process file claims: занятый scope возвращает `skipped` до изменения status или API-вызова. Stale-schema batch ставит повторно failed scopes после stale-success и untouched, чтобы постоянная ошибка с `synced_at=NULL` не создавала starvation.
- Соблюдайте актуальные WB Content API rate limits общим process-wide limiter и pacing. Не создавайте burst из одного запроса на каждую включённую категорию без bounded batch.
- Бренды WB загружаются по каждой включённой категории через актуальную cursor pagination `subjectId + next`, а не через fan-out `pattern`. Один запуск ограничен 200 запросами/страницами; полные category snapshots применяются вместе с `BrandCategoryLink`, а durable checkpoint продолжает sweep с первой незавершённой категории. Исчерпание budget, повтор cursor, неверный `total` или неполная категория не продвигают global freshness. Транспортная полнота сверяется по числу upstream items, а не только пригодных записей: элемент со строго типизированным положительным уникальным integer ID и пустым именем исключается из runtime-cache, но не делает весь snapshot неполным; невалидный/повторный ID блокирует категорию. Один canonical `MarketplaceBrand` может иметь разные WB external brand ID в разных категориях: точная identity хранится только в `BrandCategoryLink.marketplace_external_brand_id`, а legacy primary ID на binding не подставляется вместо неё. Одинаковое нормализованное имя с разными ID разрешено между категориями. Внутри одной категории такие конфликтные identity fail-closed карантинируются без выбора одного ID: для них не создаётся/не освежается available link, старый link становится unavailable, а остальные независимые строки полного snapshot продолжают применяться. Исторические links миграция не backfill-ит и свежий sweep либо live validation обязаны доказать exact ID. Rename по стабильному WB brand ID до обновления binding создаёт либо переиспользует active exact `BrandAlias` того же бренда; manual/inactive alias или canonical-name conflict не перезаписывается и fail-closed блокирует rename/freshness. Пустой complete-response сохраняет последний успешный кэш и помечает sync как failed.
- Brand refresh запускается через 360 секунд после startup и затем каждые 6 часов, использует только центральный `Marketplace.api_key`. Незавершённый checkpoint возобновляется каждые 10 минут; без `status=partial + checkpoint` resume job не вызывает WB. Scheduler/manual runs сериализуются process-wide advisory lock `BRAND_SYNC_LOCK_FILE` (по умолчанию `/tmp/seller-platform-brand-sync.lock`); не запускайте обход без этого lock и не подменяйте credential случайным seller key. DB-apply закрывает большой read snapshot до первой записи, затем каждый bounded batch заранее получает SQLite writer через `BEGIN IMMEDIATE` и коммитится без протухания prefetched ORM state; per-row savepoint не может стать неявной outer transaction. Row-local data/integrity error изолируется savepoint-ом, но SQLite `BUSY/LOCKED` является run-level contention: после первой такой ошибки весь незакоммиченный batch откатывается, checkpoint не продвигается, run становится `partial` и отдаёт writer другим задачам до следующего resume. Запрещено продолжать оставшиеся бренды, сканировать глобальные кэши и писать отдельный traceback на каждую строку. Недавний verified `BrandCategoryLink` является отдельным 48h category-scoped доказательством для агента, даже пока global sweep имеет status `partial`.
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
- Seller type search/bind читает только official `available + is_seller_selectable` дерево и не требует `is_enabled`: этот admin-toggle лишь заранее прогревает выбранную схему. После exact bind востребованность доказывается локальным FK из non-archived draft, active mapping либо available listing; singleton on-demand refresh раз в минуту не обходит типы без таких ссылок. Hard-stale tree сначала обновляется тем же отдельным admin-owned reference credential; seller operational key для глобального справочника не используется.

### Полный read-snapshot каталога Ozon

- Product list page обязан иметь strict object envelope, bounded items, integer `total`, string cursor, уникальные product/offer identities и настоящие JSON boolean. Numeric string, float и boolean не превращаются в product ID через `int(...)`; на доменной границе provider ID хранится opaque string.
- `product/info/list`, `product/info/attributes`, prices и stocks запрашиваются batch-ом для IDs текущей list page. Foreign ID, conflicting offer, повтор ID между cursor pages той же phase, изменившийся total, пустая промежуточная страница или cursor loop отклоняют всю локальную page transaction. Durable `last_catalog_sync_phase` разрешает ожидаемое пересечение `ALL`/`ARCHIVED`, но не повтор внутри одной phase.
- В БД сохраняется только whitelist-normalized bounded snapshot: статусы, ошибки, атрибуты, complex attributes, media, dimensions, barcodes и price/stock summary. Не сохраняйте произвольный raw response и не считайте удалённое Ozon `marketing_price` обязательным полем.
- `ALL` и `ARCHIVED` могут пересекаться; dedup выполняется по account-scoped offer/product identities, а `seen_count` считает уникальные local listings run-а. Archived означает существующий upstream listing (`is_available=true`, `is_archived=true`), а не missing.
- Ошибка enrichment или неполный list sweep может обновить только уже полностью подтверждённые страницы. Она не имеет права пометить unseen rows unavailable. Final missing pass дополнительно не трогает rows, созданные после `run.started_at`, чтобы параллельная публикация не была ошибочно скрыта.
- WB backfill идемпотентен по `legacy_product_id`, не удаляет/не меняет `Product`, не требует seller marketplace account и использует стабильный fallback offer `wb-nm-<nm_id>` только когда `vendor_code` пуст.

### Ozon drafts, category mapping и AI facts

- `MarketplaceProductDraft` хранит нормализованное локальное состояние, но не считается `/v3/product/import` body. Даже `validation_status=valid` не разрешает route/tool side effect: P5 builder обязан повторно сверить fact hash, account, exact category/type, schema/dictionaries, quota и полный payload.
- Fact pack строится только из seller-owned `ImportedProduct` и bounded полей его supplier source. `original_data` physical/characteristic values, legacy supplier photo URL-dicts и observed/calculated RRP fallback имеют точную provenance; processed/AI/channel images и `ai_parsed_data_json` остаются только unverified/derived и не auto-map-ятся.
- Full-product AI prompt использует policy `explicit_only`: отсутствующие значения возвращаются как `null`/`[]`, а каждый непустой факт должен иметь `parsing_meta.field_provenance`. Оценка веса/размеров/комплектации и default упаковка `20×20×30` удалены; fill percentage нельзя повышать догадками.
- Category mapping — точное seller-scoped подтверждение. Fuzzy/AI result может существовать только как `suggested`; автоматическое применение разрешено лишь для `active` mapping на seller-selectable/available Ozon type. Смена типа очищает старые attributes/complex groups, явный список удаления live-атрибутов и требует новой validation.
- Повторный mass sync для существующего draft сначала missing-only гидратирует legacy observed snapshot по exact supplier FK, затем выполняет deterministic three-way source rebase: прежнее default-значение следует свежему источнику, seller edit сохраняется, а ambiguous/complex groups не переписываются автоматически. Для ещё нетипизированного неопубликованного draft после этого повторно проверяется только `active` exact mapping: type-scoped attributes детерминированно перестраиваются из fresh observed facts, complex groups очищаются. Уже выбранный тип и active operation не подменяются; published draft после validation идёт в отдельный full-state update, а не считается автоматически загруженным.
- Если exact тип выбран до появления fresh schema/dictionaries, массовый `ozon_bulk_upload` хранит item как активный `waiting_reference`, а не terminal failure. После bounded on-demand refresh scheduler сам повторяет только локальную подготовку, добавляет отсутствующие deterministic source-backed flat attributes без перезаписи seller values/content/complex groups, валидирует и создаёт durable queued operation; второй клик не нужен. Ни одного provider write до committed operation нет. Ожидание дольше 6 часов становится явным retryable `ozon_reference_sync_timeout`; выключенные general/manual flags замораживают продолжение, но не превращают его в ошибку.
- Draft update требует typed positive `expected_version`; boolean/float/numeric string в JSON не coercion-ятся. Source drift не перезаписывает user fields: validation возвращает `source_facts_stale`, а отдельный refresh меняет только fact snapshot/provenance.
- Dictionary value валидируется как exact `(product_type_id, attribute_id, external_value_id, display value)` по fresh official cache и optional admin restriction. Required/complex/max-count/type semantics проверяет Python; неизвестный data type блокирует draft. Значение другого category/type scope не переносится.
- Replace-style update по умолчанию сохраняет все представимые live-атрибуты. Исключение — только seller-confirmed `MarketplaceProductDraft.attribute_removals_json`: bounded exact пары `attribute_id/complex_id`, которые platform-native mass editor выводит из текущих allowlisted schema-validation errors и показывает продавцу списком. Для create список запрещён. При update он удаляет только совпавшие identity из fresh exact-account baseline и локального overlay; неизвестная/исчезнувшая identity, смена типа либо snapshot drift блокирует запись. Required атрибут после очистки обязан быть заново заполнен и пройти обычный exact dictionary/type validator. Multiple barcodes, отсутствующий type ID и непредставимый raw attribute этим механизмом не обходятся. Operation summary хранит только число явных удалений, а prior full-state fingerprint/snapshot по-прежнему включает исходные live-атрибуты. Колонку добавляет fail-fast `migrate_add_marketplace_draft_attribute_removals.py`; base `migrate_add_marketplace_drafts.py`, выполняющийся раньше dedicated migration, обязан additive-добавлять legacy-missing column и только затем проверять schema.
- Physical fields принимаются только как положительные явные values с units; price должен происходить из observed/calculated product fact либо явной правки карточки. Для текущего rollout `currency_code=RUB` подставляется детерминированно, VAT — только из явного account default либо явной карточки; догадка о ставке запрещена. Media принимает только bounded public HTTP(S) URLs; `images360` отклоняется как удалённое поле. `offer_id` обязателен и уникален внутри account.
- Запрещённый Ozon-бренд — pre-write validation failure независимо от того, пришёл он из observed source, текущего canonical brand или seller-edited brand attribute. Политика exact-normalized и общая для single/bulk create/update; нельзя обходить её прямым enqueue, auto-publish или заменой регистра/пунктуации. Ошибка одной карточки не блокирует остальные строки batch.

### Ozon publication operations

- `MarketplacePublicationService.enqueue_bulk_publications` и `enqueue_bulk_updates` — внутренние chunk-и до 50 exact seller-owned готовых черновиков одного кабинета: create требует отсутствующий upstream offer, update — exact linked listing и reconstructable full state. На каждый валидный черновик создаётся committed `MarketplaceOperation(queued, next_poll_at=now)` с серверным idempotency key БЕЗ provider-вызова; отправку выполняет существующий scheduler (`poll_due_operations`, allow_submission по feature flags) под account claim с live preflight/квотой/single-attempt. В due-batch уже attempted `submitting|submitted|polling|uncertain` всегда идут раньше новых `queued`, чтобы массовая отправка не вытесняла подтверждение начатых writes. Ошибка одного черновика не блокирует остальные; активная операция по черновику даёт skip без дубля. Legacy route `POST /marketplaces/drafts/bulk-publish` остаётся для compatibility/API, а основной UI отправляет до 200 товаров/черновиков в единый подтверждённый sync-run, явно различает create/update/no-change и показывает поштучный итог. Завершённый run открывает основной platform-native editor: групповой exact type/mapping, массовое заполнение совместимых полей, boolean select, type-scoped dictionary autocomplete, полный bounded список причин и явная массовая очистка только показанных несовместимых legacy attributes. «Сохранить и проверить» меняет локальные drafts под account lock и не создаёт operation; XLSX остаётся необязательным offline fallback.
- Current `/v4/product/info/limit` может возвращать cap-only `operation_limits[{limit,limit_type}]` вместе с `daily_create|daily_update.{limit,usage,reset_at}` и `total.{limit,usage}`. Cap нельзя выдавать за remaining: доступность вычисляется по daily/total counters, а cap используется только как размер операции; старый shape с явным usage остаётся поддержан, cap-only без counters fail-closed. Exact offer lookup `/v3/product/list` и exact price lookup `/v5/product/info/prices` могут вернуть непрозрачный непустой cursor даже при полной странице; полнота доказывается exact identity set и `total == len(items)`, а не пустотой cursor.
- User route не вызывает Ozon напрямую и до постановки create/update требует явное подтверждение full-state write. Create path: seller scope → source rebase → exact completeness/full draft validation → whitelist builder → committed operation/snapshot → live absence preflight → create quota → adapter write → task reconciliation. Update path дополнительно требует linked listing exact identity, fresh reconstructable local full-state baseline с обязательным fingerprint, independent exact live fingerprint preflight и update quota; drift после catalog sync блокирует write, а exact equality с desired full payload завершает operation как `already_current` без write.
- Create создаёт только отсутствующий `offer_id`. Найденный upstream offer, включая archived, завершает operation ошибкой до quota/write; update существует только как отдельные `product_update|product_update_rollback` kinds и никогда не маскируется create path.
- `submitting` и `attempt_count > 0` выставляются и commit-ятся до вызова write adapter. Transport/5xx/malformed success после этого считается ambiguous; повтор `/v3/product/import` запрещён. Definitive validated API rejection может стать `failed`.
- Poll/status response обязан совпасть exact-set по offer, иметь bounded items/errors и известные statuses. Неполный, foreign, duplicate или malformed response не считается успехом. После 24 часов неудачного task polling automatic retry прекращается с видимым `uncertain`; ручной poll остаётся возможен.
- Update task status `imported` сам по себе не является success: info, attributes, base price и current pictures обязаны снова сложиться в exact submitted fingerprint. Единственное узкое provider-equivalent исключение — текущий Ozon attributes read не round-trip-ит обязательный import-only `8229` («Тип»): omission допускается только для одного simple-атрибута из exact submitted payload, когда свежая official type-scoped dictionary row, schema hash/version и `MarketplaceProductType.name` совпадают с ним дословно. Pre-write такой state считается `already_current`; post-write — success только вместе с подтверждённым `imported` task ID. Ambiguous reconciliation без task ID, изменённое/возвращённое другое значение `8229`, любой второй пропуск либо любое другое поле остаются third-state `uncertain`. Confirmed fingerprint и local listing всегда фиксируют фактический live read без синтетического `8229`, request fingerprint остаётся exact submitted.
- Live reconciliation без task id для create разрешена только когда committed before-state доказывает отсутствие offer до write. Для update она сравнивает exact prior/submitted/third state. `uncertain` сохраняет credentials; audited manual stop освобождает только local quota и оставляет outcome неизвестным.
- Выключение `MARKETPLACE_OZON_PUBLICATION_ENABLED` запрещает новый write и отправку безопасной queued operation, но не отменяет уже начатую сверку. Disconnect не может удалить ключ, нужный для reconciliation.
- Create compensation архивирует exact product только при unchanged full-state. Update compensation создаёт второй explicit operation и восстанавливает prior full payload только при submitted-state drift gate. Для provider-omitted `8229` restore payload переносит только тот же доказанный import-only official value из submitted operation; все round-trip-поля берутся из exact prior live. Rollback объявляется `available` лишь если такой prior payload целиком проходит текущую official required-attribute schema; например, состояние до появления обязательного ТН ВЭД остаётся аудируемым, но automatic rollback получает `unavailable` и не отправляется. Повтор create/update rollback с тем же idempotency key ищет и возвращает exact child operation до проверки текущего `rollback_status`/archived state, поэтому уже завершённый rollback никогда не превращает сетевой retry в новый write или ложный conflict; foreign parent остаётся conflict. Media — replace-style часть полного payload: `primary_image + images <= 30`, optional `color_image`, `images360` запрещён; picture read errors или непредставимый старый state блокируют write.
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

- `/v1/analytics/data` — read-only POST в endpoint manifest с capability `analytics_read`. Актуальный core-v2 request имеет только точные `date_from/date_to`, один dimension (`sku` либо `day`), фиксированные non-deprecated метрики `revenue + ordered_units`, пустые filters/sort и bounded `limit/offset`; credentials не входят в payload. `hits_view`, `hits_tocart`, `conv_tocart_percent`, `delivered_units`, `cancellations` и `returns` больше не отправляются: live API отклоняет их как `deprecated metrics used`. Их отсутствие является unavailable signal, никогда не нормализуется в наблюдённый ноль и не создаёт performance-причины quality.
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
- `ozon_fulfillment_contracts` строит периоды не длиннее 31 дня, ограничивает обе posting families (FBS v4/FBO v3) 100 строками, явно отключает posting analytics/financial data/barcodes и принимает их только как bounded top-level `postings + has_next + opaque cursor`; offset/result envelope запрещён. Пустая rFBS returns page может завершаться без `last_id`, но непустая обязана вернуть cursor. Duplicate posting/product/event identity, пустой/повторный cursor при `has_next`, malformed timestamp/decimal/boolean или over-limit response отклоняют страницу до ORM write.
- Лимиты posting pages задаются раздельно по текущему endpoint contract: FBS v4 — не более 100 строк, FBO v3 — локальный bounded cap 1000. Не объединяйте их обратно в общий лимит: upstream FBS отклоняет значения больше 100 до чтения данных.
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
- Live non-retriable Ozon denial с provider code `7` считается endpoint-level subscription/access state, а не доказанным отсутствием роли или поломкой всего кабинета. Failed sync хранит sanitized `ozon_inbox_access_denied`; scheduler не повторяет тот же `account + source_kind` 24 часа, но seller-facing ручная read-only перепроверка остаётся доступной и успешная попытка автоматически снимает cooldown.
- `/marketplaces/reviews` и API принимают account scope только canonical query-параметром. UI разделяет WB и Ozon, отзывы и вопросы, показывает capability/Premium state и явно сообщает, что ничего не отправлено. Scheduler раз в 15 минут выбирает максимум две capability-proven `account + source_kind` пары и читает максимум три страницы каждой; feature flag выключает новые provider calls, но bounded local retention cleanup продолжает удалять просроченный customer text.
- P10A не регистрирует review/question write endpoints, adapter methods, capability writes или кнопку отправки. AI/template создают только `MarketplaceReplyDraft(status=draft)`; provider send всегда `false`. Пустой отзыв без text/photo/video не получает draft. Один AI draft использует seller profile, `AIConfig.max_retries=1`, bounded customer text/facts и output cap 500 tokens; `log_payloads=false` запрещает prompt/response/provider-body debug logs. UI явно сообщает о передаче этих данных настроенному AI-провайдеру, customer text и listing facts маркируются как data/untrusted instructions, а HTML/link/email/phone/discount/compensation output отклоняется. Перед commit source/facts fingerprints перечитываются; concurrent drift отклоняет результат, а partial unique active-draft index превращается в явный conflict. Template mode полностью локален. Любая будущая отправка требует отдельного audited proposal/confirmation/idempotency/reconciliation этапа.

## LLM policy, budgets и prompt cache

- Новая seller AI-настройка создаётся с `provider=deepseek`, primary model `deepseek-v4-pro` и `agent_single_model=false`. Orchestration task types `plan_request`, `smart`, `custom`, `pipeline` используют seller primary model, обычно DeepSeek Pro.
- Internal execution skills используют DeepSeek Flash с `thinking.type=disabled`, если `agent_single_model` не включён. Pro-планирование сохраняет provider-default thinking. Никогда не переносите key/base URL между providers при fallback/model switch.
- Seller-scoped AI profile имеет приоритет. Credentials передаются task-scoped через authenticated internal API и не записываются в логи.
- Default budgets определены в `agents/config.py`: `AGENT_RUN_TOKEN_BUDGET=30000`, `AGENT_RUN_API_BUDGET=24`, `AGENT_MAX_PRODUCTS_PER_RUN=200`, `AGENT_OBSERVATION_MAX_CHARS=1200`. Изменение defaults требует тестов и обновления этого файла.
- При исчерпании budget возвращайте честный partial result без дополнительного LLM call. `llm_retry` считает каждую физическую попытку в `usage.api_requests`, а execution-path ограничивает retries фактическим остатком API-бюджета; параллельные чанки заранее делят общий лимит и не могут превысить его суммарно. Сохраняйте cancellation checks и durable skill-boundary checkpoints.
- Для больших наборов используйте prefetch, bounded chunks, batch endpoints и bounded concurrency. Не создавайте N+1 DB/API/LLM calls.
- Если точный parser уверенно извлёк явно названные поля контента, его `title|description` mask является верхней границей и semantic planner не может её расширить. При miss или опечатке planner может выбрать только значения из того же закрытого enum; свободное имя поля не принимается. `content-writer` принимает максимум 100 typed positive integer IDs без coercion из boolean/float/string, делает один content-brief query, затем Flash chunks: до 24 карточек для title-only и до 8 для description/both, дополнительно ограничивая prompt примерно 12 000 символов. Stable system prompt требует удалить из title значение `brand`, повторы/синонимы, рекламные слова и лишние подробности, перенося в description только уже подтверждённые факты. Каждый чанк обязан вернуть точное множество уникальных integer IDs и все поля; JSON schema задаёт bounded `minLength/maxLength`, а Python до stop-word/write повторно отклоняет title с любым объективным hard issue из `analyze_wb_title`. Stop-word response обязан полностью совпасть по `(product_id, field)` и после фильтрации проходит ту же length/title-проверку. Любой пропуск, дубль, чужой ID, WB-title defect или неполная проверка блокирует запись чанка без LLM retry/ReAct fallback. Product/ImportedProduct сохраняются batch endpoint-ами с optimistic `expected_updated_at`, snapshots/history и честными changed/unchanged/failed counts. Если фоновая синхронизация изменила только `updated_at`, runtime один раз перечитывает brief и повторяет тот же уже проверенный diff с новым optimistic timestamp без второго LLM-вызова; реальное изменение title/description блокирует перезапись как conflict.
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
- Scheduler job `supplier_catalog_enrichment` раз в минуту возобновляет durable admin runs общей `SupplierProduct`; один tick берёт до двух runs и до трёх bounded chunks на run, а per-supplier file claim сериализует его с immediate web kick. Не заменяйте этот поток синхронным full-selection loop внутри HTTP request.
- Scheduler job `marketplace_media_publications` каждые 15 секунд выполняет только bounded recovery/process/reconciliation durable media operations. Due `reconciling|uncertain` всегда имеют приоритет над новым `queued` write, чтобы массовая очередь не вытесняла read-after-write safety. Ни web confirm, ни restart не имеют права повторять provider write после committed `attempt_count=1`; такой operation сначала становится `uncertain` и сверяется live.
- P11 scheduler job `maintain_marketplace_projection` выполняет только local SQL, максимум три seller scopes и максимум 200 WB rows на backfill/parity batch. Не заменяйте keyset cursor на `fetchall()` и не выполняйте full-catalog backfill в startup transaction.
- Scheduler job `maintain_marketplace_source_links` также выполняет только local SQL: максимум три connected Ozon account scopes и 200 listing rows за tick. Durable cursor лежит в bounded `BackgroundJob.progress_data`; batch продвигается по `MarketplaceListing.id`, поэтому unmatched/ambiguous строка не создаёт starvation, а restart не начинает полный каталог заново. Persistent failed scope не создаёт новую job раньше чем через 10 минут; completed unresolved sweep пересматривается не раньше чем через 6 часов. Materialization сериализована `try_marketplace_source_link_lock(seller_id)` в поддерживаемой single-host/shared-filesystem topology.
- Исторический прямой контракт `migrate_add_marketplace_listings.py DB` оставлен full-backfill для совместимости уже развёрнутой migration; каждый automated startup/comprehensive path обязан явно передавать `--backfill-limit 200`/`STARTUP_BACKFILL_LIMIT`. Остаток всегда завершает durable runtime, а не startup loop.
- TLS verification можно ослаблять только внутри контролируемой локальной/Docker-сети. Не переносите insecure defaults на внешний agent endpoint.
- Docker healthcheck обязан задавать внутренний network timeout короче container timeout и закрывать ответ. Web `start-period=600s` учитывает fail-fast startup migrations на многогигабайтной SQLite БД (последний измеренный прогон — 451s); не сокращайте его ниже измеренного migration startup без отдельной проверки. Agent liveness обновляется локальным потоком без I/O и не зависит от доступности platform heartbeat/poll.
- Gunicorn запускает несколько web workers, поэтому APScheduler выбирает один процесс через advisory file lock `SCHEDULER_LOCK_FILE` (по умолчанию `/tmp/seller-platform-scheduler.lock`). Workers без lock проверяют возможность takeover каждые `SCHEDULER_LOCK_RETRY_SECONDS` (по умолчанию 15 секунд), чтобы graceful reload не оставил процесс без scheduler. Не удаляйте этот lock и не запускайте второй scheduler в том же контейнере; для нескольких web containers нужен отдельный singleton scheduler runtime.
- Web container запускает gunicorn gthread с `GUNICORN_WORKERS` (default 2) × `GUNICORN_THREADS` (default 8). Не уменьшайте пул до единичных слотов: медленные проксирующие запросы (фото поставщика) при 2×2 забивали все слоты и платформа висела целиком. Число процессов согласовано с advisory scheduler lock; масштабируйте потоками.
- Фото-прокси (`routes/photos.py`) обязан оставаться bounded: auth-cookies поставщика кэшируются в памяти процесса с TTL (`AUTH_COOKIE_TTL_OK`/`AUTH_COOKIE_TTL_FAIL`), а каждое скачивание с CDN укладывается в общий wall-clock бюджет `PHOTO_FETCH_TOTAL_BUDGET` (превышение отдаёт placeholder). Не добавляйте новый внешний fetch в request path без такого дедлайна: `requests timeout=N` ограничивает только отдельную socket-операцию, а не весь запрос, и капающий upstream держит поток минутами.
- Projection batches дополнительно сериализованы DB lease, но Ozon account/reference/publication/commercial locks остаются filesystem claims. Поддерживаемый production topology P11 — один host/shared lock filesystem; multi-host rollout запрещён до distributed account lock.

## UI и темы

Актуальная визуальная система называется «Тёплая редакция». `templates/base.html` является единственным источником palette tokens.

- Поддерживайте обе темы через `data-theme="light|dark"` и сохранённый ключ `sh-theme`.
- Используйте CSS variables `--bg`, `--bg-card`, `--bg-hover`, `--text`, `--text-secondary`, `--text-muted`, `--accent`, `--accent-strong`, `--accent-light`, `--border` и semantic status tokens. Не добавляйте отдельную несвязанную palette.
- Используйте `Inter` для рабочего интерфейса; `Instrument Serif` оставляйте для редких display-акцентов.
- Новый UI должен работать в Jinja2 + Alpine.js + существующем Tailwind CDN setup. Не вводите bundler без архитектурного решения.
- Тестовая витрина каталога `/marketplaces/listings/beta` и beta-деталь `/marketplaces/listings/beta/<id>` (routes `beta()`/`beta_detail()`/`groups_api()` в `routes/marketplace_listings.py`, `templates/marketplace_listings_beta.html`, `templates/marketplace_listing_beta_detail.html`, `static/marketplace-catalog-beta.{css,js}`, `static/marketplace-detail-beta.js`) — эксперимент постепенной миграции страниц на Vue 3 через CDN без бандлера. Обе страницы read-only поверх существующих listing JSON API плюс существующие POST: Ozon sync и link `reconcile-link`/`unlink` (с confirm и `expected_link_version`); Vue-разметка живёт внутри `{% raw %}`, дизайн — только токены «Тёплой редакции». Групповой режим «Товары» использует `GET /marketplaces/listings/api/groups` (`MarketplaceListingService.list_catalog_groups`): группировка строго по exact `imported_product_id` (никакого fuzzy), несвязанные листинги остаются одиночными группами, членство группы считается в тех же фильтрах, bounded 6 листингов на группу. Сериализатор `MarketplaceListing.to_public_dict()` отдаёт bounded `primary_image`/`hover_image` через `preview_image_urls()`: media snapshot, а для WB-проекций без media — слоты `Product.photos_json`, развёрнутые в CDN URL (`services/wb_media.normalize_photo_urls`; в list-query — `joinedload(legacy_product).load_only(nm_id, photos_json)` против N+1). Публичная ссылка строится только для WB по `nm_id`; фабриковать публичный Ozon URL запрещено. Классические страницы не изменены. Не добавляйте на бету write-действия в обход существующих proposal/confirm контрактов и не переносите другие страницы на Vue без отдельного решения.
  - `static/marketplace-beta-shared.js` (`window.mcatShared`) — единственное место, различающее две наблюдённые формы `price_summary_json`: Ozon пишет вложенный `values{price, old_price, marketing_seller_price, min_price}`, а WB-проекция (`services/marketplace_rollout.py`) — плоский `{price, discount_price, source=legacy_wb_projection}`, где `price` это цена до скидки, а `discount_price` — к оплате. Обе формы являются фактом площадки и не нормализуются в БД; любой новый consumer обязан понимать обе либо явно объявить, что работает только с одной. Там же живут карты provider-статусов/видимости, единицы габаритов (неизвестная единица показывается как есть, а не выдаётся за граммы/см) и `readJson`, который отличает истёкшую сессию от пустого ответа.
  - Счётчики фасетов считает `GET /marketplaces/listings/api/facets` (`MarketplaceListingService.catalog_facets`) двумя `GROUP BY` вместо 5+N запросов `per_page=1`; статусы считаются без статус-фильтра, каналы — без канального. Поиск не полагается на SQLite `lower()`, который не сворачивает регистр кириллицы: `_filtered_listing_query` ищет по нескольким регистровым вариантам запроса. `GET /marketplaces/listings/beta/<id>` с `Accept: application/json` отдаёт единый bootstrap страницы (listing + members + gallery + attribute_names + product_link), поэтому после `reconcile-link`/`unlink` каналы товара пересобираются без перезагрузки. `group_members` не может потерять текущий листинг из среза, а занятый seller-lock возвращает `busy=true` и не выдаётся за «совпадений не найдено».
  - Flask-Login `unauthorized_handler` отдаёт JSON-клиентам `401 {code: auth_required}` вместо HTML страницы логина: иначе любой fetch получал 200 с HTML и интерфейс показывал «данных нет» вместо «сессия истекла».
- Seller-facing Ozon считается штатным каналом: временный pilot-banner и cookie `ozon_pilot_notice_v1` удалены. Operational status показывается в кабинетах, массовых загрузках и task tray; не возвращайте глобальный dismissible pilot-banner без нового продуктового решения.
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
  `--z-dropdown/-sticky/-backdrop/-overlay/-toast/-cmdpal`.
- Графики токенизированы: `--chart-1..6` (light+dark, dataviz-validated на CVD/контраст) —
  единственный источник цветов серий; НЕ используйте status-токены и не хардкодьте hex
  в конфигах Chart.js. Helper `window.shChart` (sh-ui.js): `palette/color/fade/textMuted/grid`
  + `register(chart, recolor)` перекрашивает графики по событию `sh-theme-change` (его шлёт
  `toggleTheme`). Свотчи легенд в разметке идут через `var(--chart-N)`, чтобы совпадать с
  сериями. Затронуты `analytics.html`, `finances.html`, `finance_detail.html`.
- ⌘K-палитра ищет товары: `GET /api/products/search?q=` (login-scoped, tenant, reuse фильтра
  `vendor_code/title/brand/nm_id`, limit 20). `shCmdPalette` делает debounced fetch и рендерит
  группу «Товары» рядом со статическими разделами; клавнавигация по объединённому списку.
- Трей фоновых задач: `GET /api/tasks/tray` — read-only агрегатор активных операций из
  seller-scoped таблиц (BackgroundJob/AgentTask/AutoPublishRun/ImageGenerationExperiment/
  InfographicCampaign/MarketplaceMediaPublication/EnrichmentJob/PriceChangeBatch +
  `Seller.api_sync_status`), нормализованная форма
  `{kind,title,status,progress,started_at,link}`, per-source try/except, ничего не мутирует.
  `Alpine.store('tasks')` (адаптивный поллинг) + `partials/tasks_tray.html` (поповер у
  колокольчика, показывается только при активных задачах) смонтирован в топбар и мобильный хедер.
  Сторы `tasks` и `notif` не опрашивают бэкенд на скрытой вкладке
  (`document.hidden` gate + немедленный refresh на `visibilitychange`); не
  добавляйте новый общий поллинг без такого гейта.
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
- Каналы продаж — единый **channel bar**: макрос `channel_bar(...)` в
  `macros/components.html` + классы `.sh-channel-bar`/`.sh-channel-tab`/`.sh-mp-dot`
  в `static/sh-ui.css`. Данные берёт из Jinja-глобала `mp_nav()`
  (`services/marketplace_nav.py`, регистрируется в `seller_platform.py`) — именно
  глобальная функция, а не context processor: импорт макроса без `with context`
  не видит контекстные переменные. Токены `--mp-wb`/`--mp-ozon` (обе темы в
  `base.html`) используются только для точек идентификации каналов — не для
  статусов и не для графиков. Бар ставится сразу под шапкой страницы-концепта и
  при выключенном `MARKETPLACE_OZON_ENABLED` не рендерит ничего; не добавляйте
  второй селектор кабинета на страницу с channel bar.
- Мультимаркетплейсная ИА: один концепт — один пункт сайдбара, каналы
  переключаются channel bar-ом внутри страницы. Пары: `/products` ↔
  `/marketplaces/listings` (+таб «Все каналы»), `/card-quality` ↔
  `/marketplaces/quality`, `/prices/` ↔ `/marketplaces/commercial`, `/analytics` ↔
  `/marketplaces/analytics`, `/finances` ↔ `/marketplaces/finance`, `/reviews` ↔
  `/marketplaces/reviews`, `/products/sync-status` ↔ `/marketplaces/operations`,
  «Мои товары» ↔ `/marketplaces/drafts`. Отдельными пунктами (под флагом) остаются
  только channel-специфичные стадии: Каталог маркетплейсов, Черновики Ozon
  (группа «Поставщики»), Операции Ozon (группа «Операции»), Заказы Ozon
  (внутренние табы заказы/возвраты/отмены), Кабинеты Ozon. «Мои товары» показывает
  колонку «Каналы» (WB-статус + бейдж черновика на кабинет) и bulk «Подготовить
  Ozon». Не добавляйте новые пункты вида «X Ozon» для концепта, у которого есть
  WB-страница — расширяйте channel bar.

## Coding conventions

- Сначала прочитайте соседние route/service/model/tests и следуйте локальному паттерну.
- Сохраняйте UTF-8. Имена кода преимущественно английские; пользовательский текст и доменные комментарии могут быть русскими.
- Держите routes тонкими: auth, parse, service call, response. Транзакции и доменные правила должны быть тестируемыми.
- Используйте SQLAlchemy queries и structured parsers. Не собирайте SQL/JSON/URLs небезопасной конкатенацией.
- На exception после изменения session вызывайте `db.session.rollback()` или откатывайте savepoint.
- Не держите SQLite write-транзакцию через сетевые вызовы или sleep. Write-транзакция открывается первым flush (включая autoflush при любом SELECT после незакоммиченного изменения) и живёт до commit/rollback; удержание дольше `busy_timeout=30s` роняет параллельных писателей с «database is locked». Фазируйте sync-циклы: сначала вся сеть (при грязной сессии — под `db.session.no_autoflush`), затем запись и commit. Изоляция ошибки одной строки батча — `with db.session.begin_nested()` (savepoint), а не голый `except` без rollback: неоткаченная flush-ошибка валит весь остаток батча.
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
