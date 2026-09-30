# Подписчики deployment-бота

Владелец 26.09.2026 поручил отправлять редкие статусы и deployment-уведомления всем, кто написал боту `/start`. До изменения receiver/registry отсутствовали: обе команды отправляли только `AUTODEPLOY_TG_CHAT_ID`.

План: отдельный host-side long-poll receiver, постоянный private SQLite registry по identity бота, `/start`/`/stop`, общий single-attempt sender для agent status и autodeploy. Имена, username, текст входящих сообщений и credentials не сохраняются; нужны chat ID, подписка, checkpoint и исходы попыток. Конфликт webhook/другого receiver не разрешается удалением чужой настройки. Подтверждение offset происходит только после commit подписок. Повтор `/start` идемпотентен, `/stop` переживает restart, configured owner добавляется один раз и также может отписаться.

Рассылка читает активных подписчиков; по одному physical send на event/chat, journal резервируется до HTTP. Неизвестный исход не повторяется. Общий file lock сериализует отправителей, локальный бюджет 5 сообщений/сек и 1/сек на chat; наблюдённый 429 сохраняет pause. Платные broadcasts не включаются. Ошибка/пауза даёт честные aggregate counts, без bot token, proxy, chat IDs или raw Telegram response в stdout/journal. Receiver не импортирует Flask и не запускает deploy.

Новый systemd receiver включается независимо от основного приложения. Статусы остаются редкими по усмотрению агента, теперь сообщение адресовано всей подписавшейся аудитории: никаких личных данных, ключей, raw API или содержимого базы. Само наличие подписки не даёт доступа к Seller Hub.

Telegram [хранит неподтверждённые updates не дольше 24 часов](https://core.telegram.org/bots/api#getting-updates). Сохранившиеся `/start` будут обработаны; исчерпывающий исторический список до появления registry через Bot API восстановить нельзя. Старому подписчику, которого нет в доступных updates/registry, понадобится снова `/start`. Обычное сообщение, команда в группе и чужой `@botname` не подписывают чат.

Приёмка: unit transport/checkpoint/duplicate/opt-out/bot-scope/429/unknown/403/concurrency; реальный getMe/webhook preflight; обработка доступных updates без вывода данных; startup enabled/active receiver; одна фактическая рассылка с подтверждённым числом адресатов. Автодеплой не запускается ради проверки сообщений.

Выполнено 26.09: 20 tests; receiver enabled/active, два подписчика, общая рассылка подтверждена 2/2. [Операционный отчёт](../operations/2026-09-26-deploy-telegram-subscribers.md). При перезапуске watcher также добавлен fail-closed guard незавершённого worktree: dirty или недоступный git status запрещают автоматический pull/build/deploy.
