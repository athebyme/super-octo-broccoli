# Передеплой UX-01 — 30.09.2026

Статус: UX-01 и исправление startup опубликованы и healthy. Основание: прямое указание владельца «передеплой».

## Источник

Чистый локальный release worktree `codex/ux-01-release-20260930` основан на принятом UX commit `0b65cde`. Актуальные runtime-файлы Ozon основного дерева сверены с `ba63371`; на момент подготовки изменились только документы Ozon/price policy. Они перенесены без изменения runtime и без записи в чужое dirty main. Новая фиксация source manifest и полные offline gates выполняются для итоговых файлов. Эта локальная ветка содержит чужие snapshot-коммиты; целиком её в main не переносить.

Прежний runtime image сохранён для отката: `sha256:d7d3f54dbea423867828b9ea714e777929d4893fb59521e841bba25b08d80fda`. Production volume — `super-octo-broccoli_seller_platform_data`, не host `data/seller_platform.db`. Auto-publish=0, commercial writes=1 сохраняются.

## Проверки

Итоговые результаты перечислены ниже; новая резервная копия и restore rehearsal отменены владельцем. Offline приёмка, runtime build, production startup и ограниченный read-only smoke выполнены. Реальные публикации, изменение цен/остатков, AI pilot и API E2E не входят в проверку UX и не запускаются. Покупательская цена/скидка Ozon остаются неизвестными; незавершённые интеграционные сценарии не объявляются завершёнными.

## Уточнение владельца перед переключением

Владелец повторно поручил немедленный передеплой и явно отменил новую резервную копию из-за диска. Автоматически начавшийся daily backup остановлен; только его непринятый private temp удалён. Прежние accepted archives и production DB/WAL/ledger сохранены. Новая репетиция на восстановленной 13 GiB копии не выполняется; применяется штатный fail-fast startup guard. Это оставшийся operational риск, а не выполненная проверка.

Runtime candidate: `sha256:41d6384c0e59c3dcc053ed9360553cb6f2a347b0e3799e5261e89ac3e6cc7cb2`, source manifest `52c23165f72335628b20be3db3287d5a5be0a7f6a2b62a37c129b55c8f4c5a94` (1207 files); image сам проверен по manifest, `.env`/production DB отсутствуют. Runtime/test/CI source совпадает с полностью принятой UX/Ozon версией v6; относительно прежнего manifest обновились только 3 документа. Новый UX gate 6/6 passed, 109 tests +38 subtests/493 layouts. Полный Ozon gate v6 12/12, 1918 tests +482 subtests/93 browser scenarios/444 layouts принят до подготовки; результат дополнительного повтора Ozon doc-only manifest описан в следующем разделе.

## Дополнительный повтор Ozon

Doc-only release повтор: backend 1917 passed +482 subtests, одна contention-sensitive проверка process-shared limiter получила 38 разрешений вместо 40. Повтор полного gate остановился на этой стадии; browser stages этого дополнительного повторения не выполнялись. Изолированный повтор exact image / network none / uid1000 / CPU2 / memory1GiB / no volumes: 4 tests модуля `test_ozon_rate_limit.py` passed (0.78s). Runtime не менялся. SQLite ledger timeout0.2s и любой SQLite error дают delay60s; API client отклоняет такой запрос до HTTP. Недопуск безопасен относительно лимита; причина двух отказов не записана в первоначальном логе и не доказана, host contention согласуется с наблюдением. Предыдущий полный v6 gate того же runtime остаётся 12/12 passed.

## Расширение диска

По поручению владельца новый размер `/dev/vda` 150 GiB подтверждён. Online расширены MBR extended partition2, LVM logical partition5 (начала/типы сохранены), PV `/dev/vda5`, LV `debian12-vg/root` 98.56→148.56 GiB, ext4 через resize2fs. df: filesystem 97→147 GiB, свободно около66 GiB, reboot не понадобился. Небольшой исходный partition-table dump сохранён отдельно в private release evidence; это не копия SQLite. Явную отмену нового бэкапа не отменяли автоматически после расширения.

## Production подтверждён

Контейнер запущен 30.09.2026 17:56:10 UTC на exact image `41d6384c0e59…`, startup whole journal успешно завершён в18:06:49 UTC, к18:09:48 контейнер healthy; restart count0. Bundle `8bb293a89bff925150aa6e883cfb10181594f23f911e0b5bc5ad4f359cea6bbb`, schema `25c975bf6724f99e3046763c4b9b6faa235cecf63345ca5891141363f24b6ae1` подтверждены текущими. HTTPS login200, login-form распознан; `/static/seller-workspace.css`200 и SHA256`06ad2a44b4433601695259dd699b604782862c2c42fe185d6043023e1abeca35` совпал с source. Scheduler heartbeat healthy/progress подтверждён. Current DB/ledger/mounts/flags сохранены.

Live smoke допустил один login POST и239GET, без прочих методов и provider-capable/external requests. Admin credentials успешно вошли, но у admin нет seller profile:20 seller-specific probes недоступны (catalog403; другие redirects). Это ограничение доступа проверки, не доказательство поломки UI; seller browser matrices остаются подтверждёнными synthetic gates, production seller E2E этим входом не проверен. Привилегии/профили не менялись. Скриншотов real seller данных не создано. JSON evidence private, не в repo.

По коррекции владельца «применённые миграции не надо заново» startup fix принят и опубликован из отдельного worktree `codex/startup-migration-steps-20260930`, own commit `959178bbcfabfd3e64c86b27f10b24e062b839b2`. Текущий runtime image `sha256:4773978b6330ed0e32d40f6b59d2ad222884855e5e90d23ce7f9fbd0cd685dea` сохраняет принятую UX/Ozon основу и новый per-step journal. Production transition1.262s, healthy7.167s; старые миграции не выполнялись. Полный результат: `docs/operations/2026-09-30-startup-step-journal.md`; перенос: `docs/operations/2026-09-30-ux-01-main-handoff.md`.
