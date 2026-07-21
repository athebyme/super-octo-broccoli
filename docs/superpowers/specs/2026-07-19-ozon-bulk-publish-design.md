# Bulk-публикация готовых Ozon-черновиков

Дата: 2026-07-19. Статус: дизайн утверждён пользователем в сессии.

## Цель

Симметрия к bulk-prepare: выбранные готовые черновики публикуются одним
действием. Bulk сам ничего не отправляет в Ozon — он ставит durable
`MarketplaceOperation(status=queued, next_poll_at=now)` в существующую
очередь; минутный scheduler (`poll_due_operations(allow_submission=...)`)
уже отправляет queued-операции по одной под account file claim с live
preflight, квотой, single-attempt и reconciliation.

## Контракт

- Сервис `MarketplacePublicationService.enqueue_bulk_publications(
  seller_id, account_id, draft_ids, created_by_user_id)`:
  - `draft_ids`: 1..50 уникальных positive int (bool/float/строки отклоняют
    весь запрос);
  - каждый черновик независимо: tenant scope (`get_draft`), совпадение
    `account_id`, отсутствие `published_listing_id`, отсутствие активной
    операции, свежая полная валидация payload (`_publication_payload` с
    `expected_version=draft.version`);
  - валидный → `_create_operation` (queued, серверный idempotency key,
    без submit) + отдельный commit; невалидный → `skipped` с bounded
    причиной, остальные продолжаются;
  - результат `{queued: [...], skipped: [{draft_id, reason}]}`.
- Route `POST /marketplaces/drafts/bulk-publish` (blueprint drafts):
  gate `MARKETPLACE_OZON_PUBLICATION_ENABLED` (как ручная публикация),
  form `draft_ids[]`, редирект на список с flash-счётчиками и ссылкой на
  `/marketplaces/operations`.
- UI списка черновиков: чекбоксы у карточек, панель «Опубликовать
  выбранные (N)» с confirm, фильтр «только готовые к публикации».
- Выключение publication-флага останавливает и отправку поставленных
  queued-операций (существующее поведение scheduler-гейта) — bulk не
  добавляет нового side-effect пути.

## Инварианты (без изменений)

Один аккаунт — последовательные submit под file claim; create-only
семантика (existing offer блокирует до write); ambiguous не ретраится;
public serializer не отдаёт idempotency key; disconnect отменяет только
queued с attempt_count=0.

## Тесты

- exact-set: дубликат/бул/чужой seller/чужой account → отклонение или skip
  с причиной; валидные из смешанного набора ставятся.
- очередь: операции создаются queued с next_poll_at, submit не вызывается
  (adapter не трогается); повторный bulk по тем же черновикам → skip
  `operation_active`, дублей нет.
- невалидный черновик (missing required) → skipped с причиной валидации.
- flag off → route отказывает до сервиса.
