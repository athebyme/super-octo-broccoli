# Фоновое обновление отзывов и вопросов Ozon

## План

1. Расширить существующие `MarketplaceReadSchedule`/`MarketplaceReadRequest` двумя exact domains: `reviews` и `questions`, только с периодом `90d`. Старые analytics/fulfillment/finance сохраняют `7d|30d`, состояние, IDs, cooldowns и активные заявки.
2. Выполнить узкую атомарную миграцию CHECK constraints двух таблиц очереди. Сохранить все строки, индексы, AUTOINCREMENT high-water и FK; неожиданные зависимости/триггеры/схема останавливают миграцию. Fresh, legacy, repeated и interrupted сценарии проверяются отдельно. Сами inbox items/drafts/syncs и внешние операции не перестраиваются.
3. Подключить inbox к общему read worker: account lock, bounded single-attempt transport, persistent rate ledger, credential version, 24-часовой срок заявки и ограниченные повторы. Три страницы за шаг, без сети в HTTP handler. Автоматическое discovery учитывает capability и уже наблюдённый access-denied cooldown; новая очередь не является поводом повторять отклонённые запросы раньше срока.
4. Заменить ручной sync на enqueue/status и использовать общий Vue refresh controller. Раздел и кабинет фиксируются для каждого запроса, скрытая вкладка не опрашивает статус, unknown POST разрешается чтением durable request. После завершения перечитываются локальные обращения; ввод открытого ответа сохраняется.
5. Сделать локальный поиск по тексту, названию и артикулу Unicode NFKC/casefold, с буквальными `%/_`, без ослабления exact account scope.
6. Проверить scheduler fairness/429/denial/restart/rotation, старые три domains, миграцию, HTTP отсутствие provider I/O, обе вкладки и темы в браузере. Развернуть только конкретный прошедший приёмку образ и проверить production без повторного live inbox/price-details probe.

## Границы

Отправка ответов, тарифный доступ и buyer price этим этапом не открываются. Review/question read contracts и 90-дневная retention customer text остаются прежними. Счётчики ленты — сохранённые наблюдения, не обещание полного результата незавершённого run.

Ручная явная перепроверка может преодолеть только старый endpoint-level access-denied cooldown; provider 429/Retry-After и общий Client-Id ledger не сбрасываются. Обычное открытие страницы не создаёт заявки и не читает Ozon. Старое прямое manual read API заменяется durable enqueue, а не сохраняется вторым обходом очереди.

## Проверено на стенде

26.09: реализация пунктов 1–5 прошла 241 тест + 67 subtests; полный offline Flask/Vue стенд — 20 сценариев / 104 layouts, 19/19 фотографий. Репетиция на read-only копии реальных DDL/строк queue сохранила 3 schedules + 3 requests. [Отчёт и наблюдаемый статус деплоя](../operations/2026-09-26-ozon-inbox-durable-refresh.md).
