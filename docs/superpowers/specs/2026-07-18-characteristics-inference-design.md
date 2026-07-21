# Режим inference-предположений характеристик (characteristics_inference)

Дата: 2026-07-18. Статус: дизайн утверждён пользователем в сессии.

## Цель

Дать админскому массовому обогащению каталога поставщика третий режим: модель
видит схему предмета, source товара и **уже заполненные** характеристики и
предлагает значения для **незаполненных словарных** полей («додумывание»).
Предложения никогда не применяются автоматически — только через явное
подтверждение админа в ревью.

## Решения (утверждены)

- **Запуск**: отдельный режим `characteristics_inference` рядом с
  `category_only` / `category_and_characteristics`. Принимает только товары с
  валидной enabled leaf-категорией WB. Отдельный бюджет/статистика, можно
  запускать повторно.
- **Охват полей**: только словарные (constrained) характеристики — значение
  всегда выбирается из полного effective-словаря WB/admin. Свободный текст и
  физические величины (вес/размеры/габариты) не предлагаются никогда —
  инвариант «не выводить факты из типа товара» для них сохраняется полностью.

## Архитектура

Переиспользуется существующий каркас `SupplierCatalogEnrichmentService`
(durable run/items, chunk ≤ 6 товаров одного subject, один model-вызов на
чанк, параллельность `SUPPLIER_ENRICHMENT_LLM_CONCURRENCY`, резервация
llm-бюджета по одному вызову, file claim, cancel между чанками).

### Данные

- `SupplierCatalogEnrichmentRun.mode` расширяется значением
  `characteristics_inference` (DB CHECK — идемпотентная rebuild-миграция по
  паттерну commercial-миграции; старые rows сохраняются).
- `SupplierCatalogEnrichmentItem.inference_json TEXT` (ADD COLUMN) —
  список предложений `[{"name", "value", "rationale", "confidence"}]`.
- Items этого режима создаются сразу в `phase='characteristics'`
  (CHECK на phase не меняется); маршрутизация — по `run.mode`.

### Поток обработки чанка

1. **collect** (main thread, ORM): товары одного subject; для каждого —
   вычислить «заполнено»: merged факты фида (`characteristics_json`) +
   валидированные значения `ai_marketplace_json`. Незаполненные словарные
   поля схемы с полным списком allowed values (bounded как в constraint
   resolver: до 40 значений + truncated) идут в prompt. Если незаполненных
   словарных полей нет — item сразу `unchanged`.
2. **prompt**: schema-подмножество + source (`_product_source`) + filled map +
   инструкция: «предложи значения ТОЛЬКО для перечисленных незаполненных
   словарных полей, строго из allowed_values; для каждого — краткое
   rationale и confidence 0..1; не уверен — пропусти; это предположения,
   а не факты».
3. **parallel LLM**: как в характеристиках (Flash по умолчанию,
   `model_override` поддержан).
4. **apply** (main thread, ORM): exact-set по product_id; каждое предложение
   валидируется: имя ∈ переданному списку незаполненных словарных полей;
   значение канонизируется точным case-insensitive матчем по полному
   effective-словарю (тот же resolver, что в write-path); невалидное
   предложение отбрасывается (не валит чанк). Валидные предложения →
   `item.inference_json`, `item.status='needs_review'`, `phase='done'`.
   Карточка товара НЕ меняется. Пустой набор валидных предложений →
   `unchanged`.

### Ревью и применение

- В существующем run-экране (`admin_supplier_catalog_enrichment_run.html`)
  needs_review-строка inference-режима показывает предложения чекбоксами
  (значение + rationale + confidence).
- Новый admin-only endpoint `POST .../items/<item_id>/apply-inference` с
  списком выбранных имён полей: повторно проверяет admin role, supplier scope,
  отсутствие другого active run, source fingerprint (drift → conflict),
  свежесть схемы/словарей и повторную канонизацию каждого значения по
  актуальному словарю. Применённые значения пишутся в `ai_marketplace_json`
  (source `supplier_catalog_enrichment`, в `_meta.evidence` для каждого поля —
  `inference: approved by admin`), `content_revision`++, `before/after`
  snapshot в item — rollback работает существующим механизмом.
  Отклонение (без выбора) переводит item в терминальный `needs_review`-исход
  без изменений (кнопка «отклонить» = ничего не применять).
- Никакого auto-apply ни при каком confidence. Confidence — только сортировка
  и подсказка ревьюеру.

### Безопасность/границы

- Ozon fact pack: inference-значения не попадают в observed-факты (идут только
  существующим unverified-каналом `ai_marketplace_json` после human approve).
- Бюджеты: лимит выборки — `MAX_CHARACTERISTIC_SELECTION` (5000), общий cap
  1600 вызовов, item attempts ≤ 3 — без изменений.
- Один active run на supplier, review/rollback правила — без изменений.
- AGENTS.md обновляется в том же изменении.

## Тесты

- collect: товар без незаполненных словарных полей → `unchanged` без LLM.
- prompt builder: в списке только незаполненные словарные поля; filled map
  присутствует; физические поля отсутствуют.
- apply: валидное предложение сохраняется в `inference_json` +
  `needs_review`, карточка не изменена; несловарное/заполненное/чужое имя и
  значение вне словаря отбрасываются; пустой результат → `unchanged`.
- apply-inference endpoint: admin-only, повторная словарная валидация,
  fingerprint drift → conflict, snapshot/rollback, `content_revision`++.
- Параллельный путь: работает через тот же оркестратор (интеграционный тест
  по образцу `tests/test_supplier_enrichment_parallel.py`).
- Migration: идемпотентность rebuild, старые rows/mode сохранены.
