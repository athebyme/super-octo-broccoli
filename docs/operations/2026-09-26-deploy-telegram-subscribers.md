# Deployment Telegram: подписчики и общая рассылка

По указанию владельца подключена рассылка всем активным подписчикам `/start`. Раньше `notify_task_status.py` и autodeploy отправляли только configured chat; receiver и постоянного списка не было.

## Выполнено

- Общие transport/store в `scripts/deploy_telegram.py`, sender для статусов и autodeploy, отдельный `seller-deploy-telegram.service` с автозапуском. `/start`/`/stop`, bot identity, singleton receiver, durable offset и metadata попыток; секреты/raw ответы/личные данные в лог не выводятся.
- Обработаны два доступных update с командами, оба ответа подтверждены. Сохранены **два активных адресата**, включая прежнего получателя; повторный bootstrap не отменяет `/stop`.
- Receiver и deployment watcher **enabled / active / running / NRestarts=0**. Watcher перезапущен для загрузки общего sender. Его новая проверка dirty worktree фактически отложила автоматический pull/build/deploy; незавершённая рабочая копия не опубликована.
- **20 tests passed / 0.95s**: subscription/dedup/stop/restart, bot scope/key rotation, malformed page, singleton lock, timeout/crash reservation, 403/429, concurrent opt-out, checkpoint before offset, webhook conflict, configured owner dedup и dirty/failed git status gate. `py_compile`, shell syntax и diff whitespace checks пройдены.
- По дополнительному прямому поручению владельца отправлен один общий статус текущей работы. Telegram подтвердил **recipients=2 / delivered=2**, rejected/unconfirmed/deferred=0. Сообщение различает production и незавершённые CI/live gates.
- На этапе включения рассылки основное приложение не перезапускалось: прежний принятый image `b2fda91b09480…`, healthy / 0 restarts, старт 26.09 10:06:57 UTC.

## Границы

Для очень старых `/start`, уже исчезнувших из Telegram до появления registry, требуется повторная команда: Bot API не отдаёт полный исторический список подписчиков. Групповые команды не подписывают группу; configured group при наличии остаётся отдельным явно настроенным получателем. Это подписка на обновления, а не авторизация в Seller Hub.

Рассылка остаётся редкой и содержательной, без расписания. Один physical send на event/chat; неизвестный исход не повторяется, наблюдённый 429 сохраняет cooldown, частичная доставка не называется успешной. Внешний аварийный монитор этим изменением не добавлен. [Дизайн](../design/deploy-telegram-subscribers.md), постоянные инструкции — корневой `AGENTS.md`.

После последующей приёмки [исправления входа и повторяемой регрессии](2026-09-26-ozon-release-regression.md) отправлена ещё одна содержательная итоговая контрольная точка с общим статусом: **2/2 подтверждённые доставки**, без rejected/unconfirmed/deferred. Это отдельный результат после deployment, не повтор неизвестного send.
