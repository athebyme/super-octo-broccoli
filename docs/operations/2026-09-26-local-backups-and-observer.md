# Локальные копии и host observer — 26.09.2026

Принято **16:23 МСК**. Внешнее хранилище явно отложено владельцем; текущий пакет работает на том же хосте. App image `f7a81b581899cc064397de9faaf3b418218289051ab934a656631d0be5625cdc` сохранён, healthy/0 restarts, startup по-прежнему 12:34:51 UTC. Новый код развёрнут как host systemd services, приложение не restart-илось. Provider calls/writes и миграции этим пакетом не добавлялись.

## Что исправлено

До работы было около 15,0 GB свободного места, при необходимости почти 18,0 GB для следующего verified backup. Расписания Seller Hub backup не было. Удалены только восстанавливаемые caches: 1 364 226 048 bytes pip/Playwright browser binaries и 5 986 426 880 bytes старых JPEG (38 622 файла). Browser profile, оригинальные фото, production/rollback images, прежние архивы и DB/history сохранены. Initial free вырос до ~22,4 GB.

`seller-local-backup.timer`: ежедневно **03:15–03:20 Europe/Moscow**, Persistent catch-up. Перед копией штатный cache maintenance в отдельном процессе применяет max 1 GiB/low-water 512 MiB. SQLite helper сохранил pinned read snapshot, общий flock, archive cap 2 GiB/reserve 2 GiB/deadline 1800s и фактический round trip. Дополнительный SIGALRM ограничивает container exec 1860s. Managed rotation относится только к созданным и записанным `ownership.json` парам в `/app/data/backups/managed-daily`; после накопления остаются две последние копии, retirement допускается только после новой verified acceptance. Старые archives и неизвестные файлы не удаляются.

`seller-local-observer.timer`: ~60 секунд независимо от web/scheduler. Docker health, публичный HTTPS/TLS login, реальный scheduler flock/heartbeat, readonly SQLite size/free space, freshness/metadata копии и исход последнего backup. Bad signal подтверждается тремя последовательными samples, recovery — двумя healthy samples. Starting grace 15 минут не объявляется healthy. Активный backup определяется настоящим flock; рабочий snapshot не вызывает ложный capacity alarm, reserve <2 GiB и duration >35 минут остаются ошибками. Уведомление агрегировано, всем активным `/start` subscribers, с durable reservation/no automatic retry неизвестной доставки. Ни autorestart, ни replay provider operations нет.

## Реальная первая копия

- Snapshot: `2026-09-26T13:10:16.699511+00:00`; цикл **737.177 s**.
- Raw **13,658,419,200 bytes** → gzip **1,786,467,111 bytes**.
- Реально распакован отдельный файл, SHA/size совпали, полный restored `quick_check=ok`; manifest complete.
- Raw SHA-256: `c4776f4b907c90a8abcafc8c0725b21ae8a68efb7c1b4dc1a79ec0a9e551e483`.
- Archive SHA-256: `63ba2cfb722fed0e8a17d531e85097b1b8f060402f6052bb4af31f505ab5bec2`.
- Archive: `managed-daily/seller-platform-20260926T132232Z-ad1e576ec0f5.sqlite.gz`; private directory 0700, archive/manifest 0600.
- В managed journal сейчас **1** новая копия; вторая появится следующим успешным циклом. Прежняя проверенная копия от 09:06 UTC и остальные архивы остаются. Непринятых temporary runs после завершения нет.
- Free после приёмки **20,569,354,240 bytes**; нужно для следующей копии **17,953,386,496 bytes**. При условном втором архиве того же наблюдённого размера запас для начала третьего составил бы **829,500,633 bytes** сверх required. Это оценка на текущий момент, не гарантия ёмкости: рост БД/архивов/cache требует контроля, preflight каждого запуска остаётся обязательным. Наблюдённый минимум свободного места во время первого цикла ~6,9 GB.

## Проверки

- **101 focused tests**, 14.10s: real WAL backup/restore, manager rotation на третьей копии, failure/low space без удаления предыдущих, corruption, ownership/traversal/symlink, lock contention, active backup/capacity, state transitions/restart/stale receipt/delivery dedup, subprocess time/output budgets, существующие Telegram/heartbeat/photo regressions. Production ради alarm-test не останавливался.
- На действующем приложении после cleanup: **25 страниц/150 layouts**, 6 категорий, **60/60 фото** каталога; 0 route/JS/HTTP/overflow errors и blocked mutations. Price lanes проверены прежним read-only harness.
- Mass repair: **3 реальные партии, 6 сценариев/48 layouts, 13/13 фото**; draft versions и operation count неизменны.
- Systemd units проверены `systemd-analyze verify`, оба timer active, реальные invocations observer/backup exit 0; после завершения probe issues пуст, scheduler healthy, source backup flock свободен. Accepted app image и startup timestamp совпали.
- Новый test включён в `check_ozon_release.py`. Полный Ozon CI в этом host-only пакете не перезапускался; последний полный принятый app CI — 1375 tests +373 subtests, 23 browser scenarios/150 layouts.

Private evidence: `~/.local/share/seller-hub/releases/ozon-20260924/local-operations-release.json`, focused verification/source hashes и два browser reports. Operational state: `~/.local/share/seller-hub/local-operations`. Команды и отказные сценарии — [runbook](../OZON_PRODUCTION_RUNBOOK.md), архитектура — [дизайн](../design/local-operations-watchdog.md).

## Границы

Local observer не обнаруживает потерю всего хоста; отказ самого observer виден в systemd. Domain-specific alerts, key/media recovery, RPO/RTO, production cutover drill и долгосрочная ёмкость остаются отдельной работой. Внешнее хранилище отложено по указанию владельца. Buyer price/скидка Ozon, inbox access, реальные package/compliance facts, stock/shipping write gates и pilot этим выпуском не закрываются.

Telegram: после приёмки выполнена одна попытка рассылки каждому из **2** активных подписчиков: **1 delivery confirmed, 1 unconfirmed**, rejected/deferred=0. Повтор неизвестной доставки не выполнялся. Private receipt `local-operations-telegram.json`; частичная подтверждённость рассылки не меняет результат backup/systemd/browser acceptance.
