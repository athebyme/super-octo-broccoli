# Локальные резервные копии и независимый контроль production

26.09.2026. Следующий W8 после принятого Vue repair. Предыдущий goal turn — progress: image f7a81b581899 принят, 1375 tests/373 subtests, 23 browser scenarios, production 25 страниц + 3 repair-партии.

## Новая проверенная проблема

Production healthy/0 restarts, но source page_count 3 334 575 при page_size 4096 и свободных ~15,0 GB. Verified backup требует raw snapshot + archive cap 2 GiB + reserve 2 GiB: почти 18 GB, поэтому следующий запуск будет корректно отклонён. Расписания Seller Hub backup нет. Photo cache занимает 6,2 GB и имеет min-free только 5 GiB; 22 GB существующих архивов остаются сохранными. Это пробел capacity/operations, а не повреждение последней проверенной копии.

Владелец 26.09 явно отложил внешнее хранилище: в текущем пакете только локальный контур. Это не доказательство переживания потери всего хоста. Внешний адрес больше не блокирует этот пакет; off-host/key/media disaster recovery остаётся отложенной возможностью.

## План реализации и приёмки

1. Инвентаризировать только восстанавливаемые кэши и завершённые test resources. Освободить объём для нового verified snapshot; production/rollback images, исходные фотографии, БД и существующие архивы не удалять. Записать aggregate capacity evidence. Проверить, что фото снова загружаются после обслуживания кэша.
2. Добавить bounded stdlib probe без Flask/provider calls: действительный scheduler heartbeat, page-count/свободное место, возраст и статическая целостность metadata последней принятой копии. По состоянию manifest не утверждать новый полный restore/hash check. Активный backup подтверждается занятым существующим flock; незавершённые временные каталоги не считать копиями.
3. Host observer работает отдельно от web/scheduler: Docker running/health + публичный HTTPS /login с TLS и без redirects + внутренний probe. HTTP single-attempt, subprocess/bytes/time budgets. Начальный deployment grace соответствует длинному startup; постоянный сбой подтверждается последовательными наблюдениями, unknown не превращается в healthy.
4. Два systemd timer: наблюдение и ежедневная проверяемая локальная копия. Backup использует существующий online SQLite backup/round-trip/quick_check, неизменные 2 GiB archive/reserve budgets и общий flock. Локальная политика хранения относится только к новому управляемому каталогу и явно перечисленным созданным им файлам; старые архивы не становятся объектами автоматической очистки. Удаление управляемой старой копии допустимо только после доказанного принятия новой; минимум два последних новых snapshot. Сначала подтвердить реальный запас для такой ротации.
5. Уведомления — только подтверждённая новая проблема/восстановление, агрегировано, через существующий subscriber broadcaster; persistent state, at-most-once reservation, без auto retry неизвестной доставки. Никаких ключей, seller/покупательских данных, SQL, raw API/Telegram bodies. Один и тот же incident не повторять при каждом tick. Host failure самим host observer не обнаруживается.
6. Tests: real SQLite/locks/manifests/capacity, malformed/symlink/traversal/foreign files, reserve before cleanup, interrupted save/restore, state transitions/dedup/restart/notification failure, timer/service sandbox and bounded command invocation. Negative drills на synthetic fixtures, production не останавливать ради alarm-test.
7. Проверить свежий настоящий backup с восстановлением в отдельный файл; принять host service/timer по реальным invocations и актуальному health, проверить UI/photos. Обновить AGENTS, runbook, plan и release evidence. Сам факт установки timer не выдавать за успешно выполненный backup.

## Реализация 26.09

Код установлен на host, приложение и image f7a81b581899 не перезапускались. `local_operations.py` передаёт reviewed scripts в `docker exec` stdin. Probe stdlib-only, cache maintenance использует существующий delivery module только в отдельном процессе перед backup, без queued downloads.

- Observer: ~60 секунд, service deadline 110s; каждый subprocess ограничен временем и 64 KiB stdout во время чтения, stderr не выдаётся. Public HTTPS один раз, TLS verified, redirects/proxy environment выключены, читается bounded prefix формы входа. Docker starting grace 900s, 3 одинаковых последовательных bad samples, recovery после 2 healthy samples. Пропуск >180s сбрасывает streak. Starting не объявляется recovery.
- Backup: ежедневно 03:15 Europe/Moscow + до 5 минут jitter; Persistent catch-up. Service deadline 2050s; внутренний helper deadline 1800s и SIGALRM 1860s. Уничтожение host Docker CLI само по себе не доказывает завершение container exec: действительный source flock остаётся authority. Backup >35 минут либо failed/stale-running receipt дают signal.
- Before-copy maintenance: только JPEG cache, max 1 GiB / low-water 512 MiB в одноразовом процессе. В web environment ничего не меняется. On-demand cache продолжает заполняться; нехватка места/рост БД честно останавливают backup, observer сообщает недостаток. Initial cleanup освободил 1 364 226 048 bytes host caches +5 986 426 880 bytes photo cache; свободно ~22,4 GB вместо ~15,0 GB. Старые archives, originals, production/rollback images не удалены.
- Managed directory `/app/data/backups/managed-daily`, journal `ownership.json`. Две последние управляемые копии сохраняются после накопления; до этого прежние архивы также остаются. Только созданные и записанные manager пары допускаются к rotation после новой verified acceptance. Снятие ownership предшествует удалению: crash может оставить orphan, но не расширяет область cleanup. Unknown files и kill leftovers требуют отдельного операторского разбора.
- State `~/.local/share/seller-hub/local-operations` 0700/0600. По event сначала durable reservation, затем существующий broadcaster. Unconfirmed/reserved delivery не переигрывается; no automatic restarts/API writes. Systemd services имеют user/UMask/ProtectSystem/ProtectHome/ReadWritePaths/NoNewPrivileges/MemoryMax/TasksMax. Docker socket нужен для read probe и копии.

Приёмка реальной первой копии и browser-проверка после обслуживания cache выполнены отдельно; установка timer не считается успехом backup. Синтетический rotation-тест проверяет третью копию и сохранение двух последних по контракту. Резерв на текущем диске конечен, внешнее хранилище остаётся явно отложенным.


Принято **26.09 16:23 МСК**: actual first backup/restore 737.2s, 101 tests, 25 страниц +3 repair-партии/198 layouts/73 фото, image unchanged/healthy/0 restarts. [Выпуск и ограничения](../operations/2026-09-26-local-backups-and-observer.md).
