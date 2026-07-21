# Режим characteristics_inference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Третий режим admin-обогащения: модель предлагает значения незаполненных словарных характеристик (видя schema + source + уже заполненное), строго через needs_review и явное подтверждение админа.

**Architecture:** Переиспользуем durable каркас `SupplierCatalogEnrichmentService` (collect → parallel LLM → sequential apply). Новые части: rebuild-миграция CHECK на `runs.mode` + колонка `items.inference_json`; inference-ветки collect/prompt/validate/apply; endpoint `apply-inference` с повторной словарной валидацией; блок предложений в run-шаблоне. Спека: `docs/superpowers/specs/2026-07-18-characteristics-inference-design.md`.

**Tech Stack:** Flask/SQLAlchemy, SQLite rebuild-миграция, pytest.

## Global Constraints

- НЕ коммитить в git (рабочее дерево с чужими изменениями; деплой = rebuild из дерева).
- Деплой только после терминала текущего run `578f517f` (не рестартовать web под активным run-ом).
- Auto-apply предположений запрещён при любом confidence; физические величины и несловарные поля не предлагаются.
- Items нового режима живут в существующей `phase='characteristics'` (CHECK phase не меняется); маршрутизация по `run.mode`.
- `AGENTS.md` обновить в том же изменении. Тесты без реальных LLM/WB вызовов, `SKIP_SCHEDULER=1`.

### Task 1: Миграция + модель

**Files:**
- Create: `migrations/migrate_add_enrichment_inference.py`
- Modify: `models.py:3748-3752` (CHECK mode), `models.py` (item: колонка `inference_json` рядом с `reference_json`)
- Modify: `docker-entrypoint.sh` (fail-fast после andrey-миграции), `migrations/run_all_migrations.py` (колонка items)

**Interfaces:**
- Produces: `supplier_catalog_enrichment_runs` c CHECK `mode IN ('category_only','category_and_characteristics','characteristics_inference')`; `supplier_catalog_enrichment_items.inference_json TEXT`.

- [ ] **Step 1:** Миграция: идемпотентный transactional rebuild `supplier_catalog_enrichment_runs` (по паттерну `migrate_add_marketplace_commercial.py`): создать `_new`-таблицу с расширенным CHECK, `INSERT INTO ... SELECT` всех колонок, drop/rename, воссоздать индексы (`idx_supplier_catalog_enrichment_run_active`, partial unique active), `PRAGMA foreign_key_check` baseline до/после. Затем `ADD COLUMN inference_json TEXT` в items при отсутствии. Пропуск, если CHECK уже расширен (детект по `sqlite_master.sql`).
- [ ] **Step 2:** models.py: расширить CHECK в `__table_args__` run-а; добавить `inference_json = db.Column(db.Text)` в item.
- [ ] **Step 3:** Подключить в entrypoint (fail-fast, без `|| echo`) и `run_all_migrations.py` (items-колонка).
- [ ] **Step 4:** Тест миграции `tests/test_enrichment_inference_migration.py`: создать старую схему c CHECK и парой rows (оба старых mode), прогнать дважды, проверить: rows живы, новый mode вставляется, `inference_json` есть.

### Task 2: Сервис — inference collect/prompt/validate/apply

**Files:**
- Modify: `services/supplier_catalog_enrichment.py`
- Test: `tests/test_enrichment_inference.py`

**Interfaces:**
- Produces: `MODE_INFERENCE='characteristics_inference'`; `create_run(mode='characteristics_inference')` (лимит `MAX_CHARACTERISTIC_SELECTION`, items создаются `phase='characteristics'`, estimated_calls = ceil(n/6)+10); `_collect_inference_chunk(run_id)` / `_apply_inference_response(ctx, validation_client, response)` — той же формы, что characteristic-пара, поэтому параллельный оркестратор переиспользуется параметризацией пары функций; `_filled_characteristic_names(product, validator)` — merged заполненные имена (feed + validated ai_marketplace).
- Consumes: `_characteristic_schema`, `_product_source`, `_reserve_llm_call`, `_mark_batch_error`, `MarketplaceAwareParsingTask.parse_response` (детерминированная словарная канонизация).

- [ ] **Step 1 (тесты, красные):** по фикстурам `tests/test_supplier_enrichment_parallel.py`:
  - товар, у которого все словарные поля заполнены → item `unchanged`, 0 LLM-вызовов;
  - prompt содержит только незаполненные словарные поля + блок `filled`; физических полей нет;
  - валидный ответ `{"results":[{"product_id":N,"suggestions":[{"name","value","rationale","confidence"}]}]}` → `inference_json` сохранён, `status='needs_review'`, `ai_marketplace_json` НЕ изменился, `content_revision` НЕ вырос;
  - предложение с чужим/заполненным именем или значением вне словаря отброшено; если валидных нет → `unchanged`;
  - бюджет: резервация по одному вызову (переиспользуется существующий тест-паттерн).
- [ ] **Step 2 (реализация):**
  - `create_run`: допустить новый mode; для него `_validate_ids` использует лимит 5000; items создавать `phase='characteristics'`; estimated_calls как выше.
  - `_collect_inference_chunk`: как `_collect_characteristic_chunk`, но дополнительно строит для каждого товара `filled` (имена из `characteristics_json` + validated `ai_marketplace_json` через `validator.parse_response`) и `open_dictionary_fields` = словарные (`constraint['constrained']`) поля схемы минус filled; товары без открытых полей закрываются `unchanged`; prompt — `_inference_prompt(category, fields_by_product, sources)`.
  - `_inference_prompt`: system «Ты предлагаешь ВЕРОЯТНЫЕ значения незаполненных словарных характеристик… это предположения, не факты; выбирай строго из allowed_values; не уверен — пропусти», user — JSON payload {category, products:[{product_id, source, filled, open_fields:[{name, allowed_values, truncated}]}]}, формат ответа со `suggestions`.
  - `_validate_inference_response`: exact-set product_id; suggestions — список объектов с str name / str|list value / str rationale ≤200 / float confidence 0..1; имя ∈ open_fields товара.
  - `_apply_inference_response`: для каждого товара прогнать `{name: value}` через `validator.parse_response` (канонизация словарём); валидные → `item.inference_json`, `needs_review`; никаких записей в product; drift-гейты (source fingerprint/target) как в характеристиках.
  - `process_run`: ветка `if run.mode == MODE_INFERENCE` — те же sequential/parallel оркестраторы с parметризованной парой (collect_fn, apply_fn) (обобщить `_process_characteristic_batches_parallel` до `_process_chunk_batches_parallel(collect_fn, apply_fn, ...)`, сохранив старые имена-обёртки).
- [ ] **Step 3:** `SKIP_SCHEDULER=1 venv/bin/python -m pytest -q tests/test_enrichment_inference.py tests/test_supplier_enrichment_parallel.py tests/test_supplier_catalog_enrichment.py` — зелёные.

### Task 3: Ревью-endpoint и UI

**Files:**
- Modify: `routes/supplier_catalog_enrichment.py` (по паттерну `apply_category` route :301-330), `services/supplier_catalog_enrichment.py` (метод `apply_inference_selection`), `templates/admin_supplier_catalog_enrichment_run.html`
- Test: `tests/test_enrichment_inference.py` (дополнить), `tests/test_supplier_catalog_enrichment_routes.py`-паттерн

**Interfaces:**
- Produces: `POST /admin/suppliers/<supplier_id>/catalog-enrichment/runs/<run_id>/items/<item_id>/apply-inference` (form: `field_names[]`); сервисный `apply_inference_selection(run_id, item_id, supplier_id, admin_user_id, field_names)`.

- [ ] **Step 1 (тесты):** apply выбранного подмножества: значения канонизируются по свежему словарю, пишутся в `ai_marketplace_json` (source `supplier_catalog_enrichment`, `_meta.evidence[name]='inference: approved by admin'`), `content_revision`++, `before/after` в item, статус `applied`; drift source fingerprint → `rollback_conflict`-стиль отказ (item остаётся `needs_review`, ошибка `source_changed`); пустой выбор → item остаётся `needs_review` без изменений; не-admin/чужой supplier → 403/404; активный другой run → отказ.
- [ ] **Step 2:** сервис `apply_inference_selection`: проверки (run принадлежит supplier, item принадлежит run, `status='needs_review'`, `inference_json` непуст, нет другого active run, fresh reference/schema), повторная канонизация каждого выбранного имени, запись через тот же merge-код, что в `_apply_characteristic_response` (вынести общий helper `_merge_validated_into_product`), snapshot before/after, `characteristics_changed=True`, `status='applied'`.
- [ ] **Step 3:** route: `_admin_required`, `_supplier_or_404`, `_positive_form_ids`-аналог для имён (bounded 100 имён, каждое ≤100 симв.), редирект назад с flash.
- [ ] **Step 4:** шаблон run-страницы: для item c `inference_json` и `needs_review` — список чекбоксов `название: значение (confidence, rationale)` + кнопки «Применить выбранные» / «Отклонить». Использовать существующие `.sh-*` классы, обе темы, без inline hex.
- [ ] **Step 5:** прогнать тесты Task 2+3 файлов.

### Task 4: Документация и деплой

- [ ] **Step 1:** AGENTS.md — в раздел «Массовое обогащение…»: третий режим, только словарные поля, filled в промпте, no-auto-apply, ревью-endpoint, миграция в списке миграций.
- [ ] **Step 2:** `python -m py_compile` всех изменённых, `git diff --check`, полный набор: `SKIP_SCHEDULER=1 venv/bin/python -m pytest -q tests/test_enrichment_inference*.py tests/test_supplier_enrichment_parallel.py tests/test_supplier_catalog_enrichment.py tests/test_supplier_catalog_enrichment_routes.py tests/test_supplier_catalog_enrichment_migration.py`.
- [ ] **Step 3 (после терминала run 578f517f):** rebuild+restart web (миграция в entrypoint), smoke: создать inference-run на 12 товарах Андрея с валидными категориями, убедиться в появлении предложений в ревью; отчёт пользователю.
