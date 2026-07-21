# Bulk-публикация Ozon-черновиков Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** «Опубликовать выбранные» для готовых Ozon-черновиков через существующую durable-очередь операций. Спека: `docs/superpowers/specs/2026-07-19-ozon-bulk-publish-design.md` (дизайн утверждён).

**Architecture:** Никакого нового write-пути: сервис ставит `MarketplaceOperation(queued, next_poll_at=now)` без submit; scheduler `poll_due_operations(allow_submission=флаги)` уже отправляет их под account claim.

## Global Constraints

- НЕ коммитить в git. Инварианты publication-контура не ослаблять; bulk ≤ 50 ID, ошибка одного не блокирует остальные; idempotency key не возвращается наружу.
- AGENTS.md обновить в том же изменении. Деплой координировать с параллельной Codex-сессией (окно 502 ~5 мин).

### Task 1: Сервис enqueue_bulk_publications

**Files:** Modify `services/marketplace_publications.py` (после `start_publication`); Test `tests/test_marketplace_bulk_publish.py` (фикстуры по образцу `tests/test_marketplace_drafts.py` + существующих publication-тестов).

- [ ] Тесты (красные): смешанный набор (валидный + чужой account + уже с активной операцией + непроходящий валидацию) → queued ровно для валидного, skipped с причинами; операция `status='queued'`, `attempt_count=0`, adapter не вызван; повторный bulk → skip без дублей; >50 ID / дубли / bool → `MarketplacePublicationValidationError`.
- [ ] Реализация: сигнатура из спеки; ID-валидация как `_positive_integer` в цикле + set-дедуп с отклонением дублей; на черновик: `MarketplaceDraftService.get_draft` → account match → `published_listing_id is None` → нет active operation (query по draft_id + ACTIVE_STATUSES) → `_publication_payload(expected_version=draft.version)` → `_create_operation(idempotency_key=secrets.token_urlsafe(24), ...)` → `db.session.commit()`; исключения `MarketplaceDraftError|MarketplacePublicationError` → rollback + skipped(reason ≤200 символов).
- [ ] Прогнать новый файл + `tests/test_marketplace_drafts.py`.

### Task 2: Route bulk-publish

**Files:** Modify `routes/marketplace_drafts.py`; Test в том же файле тестов (route-уровень по паттерну legacy-моков либо сервисный вызов через blueprint client).

- [ ] `POST /marketplaces/drafts/bulk-publish`: `_seller_id()`; gate: `MARKETPLACE_OZON_ENABLED` и `MARKETPLACE_OZON_PUBLICATION_ENABLED` (иначе flash + redirect); `account_id` из form (positive int), `draft_ids` из `request.form.getlist('draft_ids')`; вызов сервиса; flash `Поставлено в очередь: N, пропущено: M` (+ первые 3 причины) + ссылка на операции; redirect на `marketplace_drafts.index`.
- [ ] Тест: flag off → сервис не вызван; happy-path счётчики.

### Task 3: UI списка черновиков

**Files:** Modify `templates/marketplace_drafts.html` (+ route `index`, если нужен фильтр).

- [ ] Alpine-выборка: чекбокс на карточках, где `draft.validation_summary.publishable` и нет `published_listing_id`; панель «Опубликовать выбранные (N)» (форма POST c hidden `draft_ids`, confirm-диалог с текстом про очередь и операции); фильтр-чип «Только готовые» (`?ready=1` в route index).
- [ ] Проверить обе темы/мобильный, jinja-компиляция.

### Task 4: Документация и деплой

- [ ] AGENTS.md: в разделы drafts/publication — bulk-enqueue контракт (≤50, queued без submit, scheduler отправляет, флаг гейтит).
- [ ] py_compile, `git diff --check`, полный прогон затронутых тестов; rebuild+restart; смок в проде: поставить в очередь 1 готовый черновик Андрея (если есть) и увидеть операцию в /marketplaces/operations.
