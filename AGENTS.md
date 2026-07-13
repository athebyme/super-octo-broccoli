# Руководство для AI-агентов

## Статус документа

Это корневой источник инструкций для автоматизированной разработки в репозитории. Он относится ко всему проекту, если более вложенный `AGENTS.md` не задаёт узкое исключение.

**Обязательное правило:** при изменении архитектуры, границ модулей, runtime-потоков, команд запуска, переменных окружения, миграций, агентной политики, safety-инвариантов, бюджетов LLM/API, prompt caching или UI-темы обновляйте этот файл в том же изменении. Не оставляйте команды и схемы работы только в коде или сообщении к PR.

## Назначение проекта

WB Seller Platform, или Seller Hub, автоматизирует работу продавца Wildberries: импорт и подготовку карточек, контент, категории и характеристики, цены, остатки, аналитику, поставщиков, фотографии и публикацию.

Основной интерфейс является Flask/Jinja-приложением. Актуальная AI-архитектура представляет одного seller-facing помощника в формате чата. Один runtime `orchestrator` строит план и вызывает внутренние типизированные skills в одном процессе. Старые отдельные agent workers сохранены только как legacy profile.

## Карта репозитория

- `seller_platform.py`: основной Flask application object, конфигурация, CLI, scheduler bootstrap и регистрация route-модулей.
- `app.py`: отдельный legacy-калькулятор прибыли, локальный порт `5000`; в текущем Compose его нет.
- `models.py`: единый набор SQLAlchemy-моделей, включая sellers, products, agent tasks, chat, proposals и change snapshots.
- `routes/`: UI и HTTP API. Новую бизнес-логику держите в `services/`, а не раздувайте handlers.
- `services/`: доменная логика, интеграции WB, поставщики, pricing, карточки, content factory и фоновые процессы.
- `templates/`: Jinja2 UI. Общая оболочка и design tokens находятся в `templates/base.html`.
- `static/`: CSS/JS без отдельного frontend build. TailwindCSS и Alpine.js подключены через CDN.
- `migrations/`: идемпотентные SQLite migration scripts. Это не Alembic.
- `scripts/`: init, backup, diagnostics и operational utilities.
- `tests/`: смешанный набор pytest-style и `unittest.TestCase` тестов.
- `docs/`: дополнительная документация. При расхождении команд доверяйте текущим Compose/entrypoint и этому файлу.

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
- `agents/catalog/`: внутренние domain skills и pipeline catalog. Это не отдельные seller-facing агенты.
- `static/agent-chat.*`, `static/ai-chat-popup.*`, `templates/agents.html`: основной чат и компактный popup.

Основной поток: browser chat -> `routes/agents.py` -> `agent_harness` -> `AgentTask` -> poll единого orchestrator -> `UnifiedSellerAgent` -> internal skill -> tools -> authenticated internal API -> DB -> conversation polling -> UI. Точные безопасные intents планируются deterministic-first; неоднозначные запросы получают semantic plan или clarification. Read-only план может стартовать автоматически, write-plan требует подтверждения.

Частые read-intents (цены, остатки, пропуски контента, import/publication status, supplier publication counts, WB catalog counts, API health, defaults, stop-words и pricing settings) обязаны сначала проходить строгий локальный parser и typed SQL/internal endpoint. Не используйте LLM как классификатор там, где intent и параметры можно проверить regex/enum. Для catalog query допускается один короткий Flash-вызов только для формулировки ответа; в него передаются condition/count/has_results, но не карточки и не история диалога. Точные supplier counts возвращаются без polish-вызова.

Явно выбранные карточки обрабатываются typed batch-путём. `batch-audit` получает до 200 IDs одним tenant-scoped query и работает без LLM. Контентный write принимает до 100 IDs, один раз загружает compact content brief, затем использует bounded Flash chunks и пакетные writes. Не подменяйте этот путь циклом GET/PATCH или отдельным LLM-вызовом на карточку.

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
python migrations/run_all_migrations.py data/seller_platform.db
```

Правила schema changes:

- Создавайте новый идемпотентный script в `migrations/`; не полагайтесь только на `db.create_all()` для существующих БД.
- Не переписывайте уже развёрнутую миграцию так, чтобы старые инсталляции получили другое поведение.
- Подключайте новый script к `docker-entrypoint.sh` и, когда уместно, к comprehensive migration path.
- Держите DDL и backfill повторно запускаемыми; проверяйте наличие table/column/index.
- Backfill обязан явно заполнять Python-side default поля вроде `created_at`: таблица, созданная SQLAlchemy, может не иметь server default даже если новый migration DDL его объявляет.
- Не удаляйте таблицы, volume или пользовательские данные без явного запроса и проверенного backup/restore plan.

## Safety-инварианты

Эти правила нельзя ослаблять ради удобства реализации.

### Tenant scope

- Любой user-facing read/write должен исходить из `current_user.seller`, а не доверять `seller_id` из body/query.
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

- Любой supplier/AI characteristic patch перед созданием или обновлением карточки проходит fail-closed проверку по WB category schema из `MarketplaceCategoryCharacteristic` и синхронизированным в админке словарям. Для материала и точных характеристик `Пол|Пол товара` обязателен category-scoped `dictionary_json`, редактируемый администратором: глобальный `kinds` может содержать `Унисекс` и не доказывает допустимость значения для конкретной категории. Для `Цвет`, `Страна производства`, `Сезон` и НДС используйте соответствующий глобальный `MarketplaceDirectory`; ТНВЭД запрашивается только с typed `subjectID`.
- Отсутствующая категория, схема или обязательный словарь блокирует отправку характеристик в WB с диагностикой о синхронизации. Нельзя молча пропускать невалидное значение и сообщать об успешном обновлении.
- В apply-path допустима только точная case-insensitive канонизация значения из словаря. Fuzzy/substring matching можно показывать как suggestion, но нельзя автоматически применять перед side effect.
- Обновление характеристик является patch по `charc_id`: проверенные значения мержатся с полным текущим массивом, чтобы частичное обновление не удалило остальные характеристики.
- Центральные single/create/batch методы WB API повторно проверяют characteristic patch. Подготовленная batch-карточка переносит typed `subjectID` и ID изменённых характеристик во внутреннем контексте, который удаляется до HTTP; прямой batch payload с характеристиками без category context запрещён. `null`/объект не может заменить массив, а пустой patch не очищает существующие значения.
- Ручные category allowlists из админки не должны стираться, когда официальный WB schema response не содержит `dictionary`. Их изменение обновляет version/hash схемы. Rollback характеристик также повторно проходит актуальные schema/dictionary checks; устаревший или недопустимый snapshot не отправляется в WB.
- Не выводите пол или материал из одной категории и не задавайте общий fallback `Пол=Унисекс`. Значение должно прийти из проверяемых данных и пройти словарь WB.

### Актуальность справочников WB

- Категории, характеристики и специальные справочники из админки являются structured truth. Агент читает их typed SQL/internal tools; не переносите эти данные в RAG, prompt constants или память модели.
- Общий refresh категорий и глобальных справочников выполняется каждые 24 часа и через 90 секунд после старта scheduler. Для включённых категорий recovery refresh запускается через 180 секунд после старта с лимитом 200, затем refresh-ahead выбирает схемы старше 30 часов пакетами до 50 каждые 6 часов. Эти jobs разделены и не должны одновременно синхронизировать один schema batch. Hard TTL для agent read/write равен 48 часам: после него данные не используются до успешной синхронизации.
- Синхронизация применяет изменения только после полного типизированного upstream snapshot. Пустой/невалидный ответ, повтор страницы, дубли ID или аномальное уменьшение snapshot не должны снимать availability либо затирать последний успешный кэш.
- Удалённые WB категории и характеристики не удаляются физически: помечайте их `is_available=false`, сохраняйте историю/настройки администратора и исключайте из новых agent choices. Обновляйте `last_seen_at`, status/error, version и hash; изменение имени, типа, обязательности, `hasFilter`, единицы, лимита или словаря считается новой версией схемы.
- Пользовательские `ai_instruction` не перезаписываются синхронизацией. Для этого хранится явный source `generated|custom`; автоматически регенерируются только generated instructions.
- WB `required=true` сильнее старого admin-флага `is_enabled`: ставшая обязательной характеристика автоматически включается обратно, всегда выдаётся агенту и не может быть отключена вручную.
- Internal reference endpoints возвращают `reference_status`. Категоризация, заполнение характеристик и нормализация размеров обязаны сделать zero-LLM preflight и вернуть проверяемый partial/clarification при `usable=false`. Write endpoints повторно валидируют freshness, availability, точные имена/ID, типы и словари, поэтому prompt-инструкция не является safety boundary.
- Batch write-path переиспользует request-level schema/directory validation cache. Не выполняйте повторный набор reference SELECT для каждой карточки одной категории.
- Соблюдайте актуальные WB Content API rate limits общим process-wide limiter и pacing. Не создавайте burst из одного запроса на каждую включённую категорию без bounded batch.
- Бренды WB загружаются по каждой включённой категории через актуальную cursor pagination `subjectId + next`, а не через fan-out `pattern`. Один sweep ограничен 200 запросами и 25 страницами на категорию; исчерпание budget, повтор cursor, неверный `total` или неполная категория возвращают `complete=false` и не продвигают freshness. Пустой complete-response сохраняет последний успешный кэш и помечает sync как failed.
- `MarketplaceBrand` доступен агенту только при одновременных `status=verified` и `is_available=true`. `pending`, `rejected` и `needs_review` остаются для админского review, но не попадают в runtime cache и internal brand validation как WB-binding.
- ТНВЭД является category-scoped справочником: `/content/v2/directory/tnved` требует `subjectID`. Не включайте его в глобальный `sync_directories`; кэш и tool для ТНВЭД должны сохранять typed category scope.

## LLM policy, budgets и prompt cache

- По умолчанию orchestration task types `plan_request`, `smart`, `custom`, `pipeline` используют seller primary model, обычно DeepSeek Pro.
- Internal execution skills используют DeepSeek Flash с `thinking.type=disabled`, если `agent_single_model` не включён. Pro-планирование сохраняет provider-default thinking. Никогда не переносите key/base URL между providers при fallback/model switch.
- Seller-scoped AI profile имеет приоритет. Credentials передаются task-scoped через authenticated internal API и не записываются в логи.
- Default budgets определены в `agents/config.py`: `AGENT_RUN_TOKEN_BUDGET=30000`, `AGENT_RUN_API_BUDGET=24`, `AGENT_MAX_PRODUCTS_PER_RUN=200`, `AGENT_OBSERVATION_MAX_CHARS=1200`. Изменение defaults требует тестов и обновления этого файла.
- При исчерпании budget возвращайте честный partial result без дополнительного LLM call. Сохраняйте cancellation checks и durable skill-boundary checkpoints.
- Для больших наборов используйте prefetch, bounded chunks, batch endpoints и bounded concurrency. Не создавайте N+1 DB/API/LLM calls.
- Явно названные поля контента извлекаются детерминированно в immutable mask `title|description`: semantic planner не может расширить эту маску. `content-writer` принимает максимум 100 typed IDs, делает один content-brief query, затем Flash chunks: до 24 карточек для title-only и до 8 для description/both, дополнительно ограничивая prompt примерно 12 000 символов. Каждый чанк обязан вернуть точное множество уникальных IDs и все поля; stop-word response обязан полностью совпасть по `(product_id, field)`. Любой пропуск, дубль, чужой ID или неполная проверка блокирует запись чанка без LLM retry/ReAct fallback. Product/ImportedProduct сохраняются batch endpoint-ами с optimistic `expected_updated_at`, snapshots/history и честными changed/unchanged/failed counts.
- Cancellation проверяется до prefetch, до и после каждого LLM chunk, перед postprocess/tool/write и перед commit. После отмены не планируйте новые futures и не исполняйте уже сгенерированные tool calls. Structured batch error не должен автоматически переключаться на дорогой ReAct; usage сохраняется в partial/failed result и checkpoint.
- Не запускайте Flash-классификатор перед каждым точным запросом. Regex/enum/typed SQL остаются первым уровнем; короткий structured Flash GoalSpec допустим только как fallback для простого неоднозначного намерения, а сложный многошаговый план строит primary/Pro.
- Task mutation endpoints (`start`, `progress`, `checkpoint`, `complete`, `fail`) возвращают только компактный статус. Полные `input_data`, `checkpoint` и `result` доступны лишь в poll/get flows и не должны эхом передаваться worker на каждом обновлении.
- DeepSeek prompt cache автоматический. Стабильные system instructions, JSON schema и tool definitions должны идти до динамического user/task content. Не добавляйте timestamps, IDs или перестановку schema/tools в стабильный prefix.
- Сохраняйте usage: input/output, API requests, cache hit/miss tokens, reasoning tokens, requested model breakdown и cost where available. Cached tokens входят в input и не должны второй раз добавляться в total. UI должен отличать `Без LLM`, Flash execution и Pro orchestration по фактическим запросам, а не по загруженному primary profile.
- `cost_usd` означает provider-reported cost; `estimated_cost_usd` хранится отдельно и требует актуальной документированной pricing table.

### Retrieval policy

- Structured seller truth (`Product`, `ImportedProduct`, defaults, categories/characteristics, pricing, stock, stop-words, API logs and live statuses) читается только typed SQL/tools и не индексируется как RAG corpus.
- Для неструктурированных правил WB и проверенных инструкций используйте selective hybrid retrieval: curated source allowlist, document version/checksum, global или tenant scope, FTS exact/trigram rank fusion и обязательные citations. Не индексируйте весь `docs/`, код, `AGENTS.md`, логи, секреты и устаревшие guides автоматически.
- Retrieval должен возвращать не более 5–8 фрагментов и примерно 6 000 символов. Точный knowledge query работает без LLM; Flash без thinking допустим только для неоднозначного query rewrite. Pro не используется для retrieval routing.
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
