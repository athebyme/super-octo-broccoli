# Подготовка оставшихся реальных Ozon-пилотов

25.09.2026, 01:00–01:05 МСК (24.09, 22:00–22:05 UTC). Production image e94c3ca4ef95… healthy. Новых внешних product/price/stock writes в этой подготовке нет.

## Товарные черновики

Текущий `_build_validation_result` вызван для всех **19** seller/account drafts при SQLite `query_only=ON`, без autoflush и с запрещённой сетью. **Publishable: 0.** Статусы строк не переписывались. В 12 черновиках изменились исходные факты; встречаются отсутствующие размеры/вес упаковки и единицы, обязательные характеристики, несколько штрихкодов, устаревший snapshot удаления атрибута.

Ближайшие существующие create-кандидаты #4/#5 требуют source rebase и подтверждённых `22232` (ТН ВЭД) / `23536` (маркировка). Значения не подставлялись по догадке. Historical `ready/valid` у #8 от июля не доказывает текущую готовность: актуальная проверка возвращает source drift, пропущенные поля и изменившийся removal baseline. Следующий UX-аудит должен проверить, не показывает ли UI старую validation как актуальную готовность.

Исторические операции #1/#3/#4 действительно имеют succeeded и по одной physical attempt, но относятся к **25 июля**; #2/#5 — no-op с attempt_count=0. Они не закрывают текущий реальный пилот новой версии интерфейса и актуальных требований.

Для старой uncertain operation #6 сначала получен account-busy до provider I/O. После освобождения lock выполнен один штатный `poll_operation(..., allow_submission=False)` с read-only adapter allowlist, retries=0 и общим rate ledger. Один physical task-status read сохранил uncertain / `ozon_task_poll_deadline_exceeded`, attempt_count=1, poll_count 2→3, next_poll_at=NULL. Повторной записи и ручного success нет.

## Подготовленный складской пилот

Через настоящий Vue UI, seller session и CSRF создана **заявка №3**, `pending_review`, operation_id=NULL:

- Артикул **1366Z1C1S21530**, listing 31301, account 1.
- Склад **X-sklad SPB**, local warehouse 1; exact upstream warehouse/SKU сохранены в proposal.
- Live baseline **5 доступных единиц → предложено 4**.
- [Просмотр заявки](https://seller-platform.tech/marketplaces/commercial/3).

Browser gate разрешал ровно один POST создания stock proposal с exact listing/warehouse/quantity, не разрешал approve. Сравнение и отсутствие установленного подтверждения проверены; 6 desktop/mobile light/dark вариантов без overflow и JS errors. Приватные screenshots/report сохранены в `ozon_release_reports/20260925-pilots`.

**Нужно решение владельца** на реальный цикл 5→4→5. По AGENTS.md price/stock применяются только после отдельного человеческого review; прежнее конкретное разрешение было на цену +25%/возврат. До ответа заявка не утверждается. Если разрешение поступит: повторно проверить точный scope/version/live baseline, отправить через штатный approve, дождаться exact read-back 4, затем подготовить отдельный rollback proposal на 5 только при unchanged submitted state, показать/проверить этот diff и применить в пределах данного разрешения. При drift/unknown исходе автоматический повтор или blind restore запрещён.

Изменение склада не заменяет отсутствующие факты карточки и не закрывает product create/update gates. Новый price-details источник по-прежнему недоступен (403), ежедневные операции и финансовые gates также открыты.

## Повторная проверка редактора #8

Read-only `MarketplaceDraftEditor.document` на production 25.09 в 01:25 МСК подтвердил: старые `ready/valid` не открывают кнопку отправки. Текущий `readiness=source_stale`, `baseline_error=true`, active operation #6 остаётся uncertain без `next_poll_at`; все три условия запрещают отправку в Vue. Гипотеза обхода готовности не подтвердилась. Отдельная UX-шероховатость: общий заголовок редактора для любой active operation говорит «Отправка выполняется», хотя именно #6 ожидает ручной сверки. Следующий UI-проход должен различить эти состояния; повторная запись не разрешена. Отчёт `editor-8-readiness.json`, 0 provider вызовов / изменений БД.
