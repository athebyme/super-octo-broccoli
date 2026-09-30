# Проверяемая штатная копия SQLite

Статус: **штатная host-side команда обновлена; свежий production backup создан и реально восстановлен для проверки; сайт healthy, 0 restarts**.

## Исправленная проблема

Старый `scripts/backup_database.sh` копировал основной файл работающей базы через `docker cp`, не обеспечивая согласованность с WAL. Проверка целостности такого файла сама по себе не доказывает, что в копию попали уже подтверждённые записи из WAL. Релизные архивы 24–25.09 создавались отдельными проверенными Python-командами через SQLite Backup API; найденная проблема относится к старому штатному shell-пути.

Команда теперь вызывает локальный stdlib helper в работающем Docker-контейнере от пользователя `app`, через stdin. Она не требует перезапуска приложения и не полагается на старую версию helper внутри image. Текущий app image остаётся `0632b1e7e6cf…`; это изменение host-side эксплуатационной команды, а не новый app deployment.

## Команды и ограничения

```bash
bash scripts/backup_database.sh

# Отдельная локальная SQLite-база; пути не относятся к Docker:
venv/bin/python scripts/verified_sqlite_backup.py \
  --database /absolute/source.db --output-dir /absolute/backups
```

Docker default source: `/app/data/seller_platform.db`; output: `/app/data/backups`. Optional `SELLER_BACKUP_CONTAINER` выбирает контейнер. `--database` и `--output-dir`, переданные shell-команде, являются путями внутри выбранного контейнера.

По умолчанию cap gzip — 2 GiB, reserve — 2 GiB, общий timeout — 1800 секунд. До копирования требуется место под raw size + весь archive cap + reserve. Source открывается read-only, snapshot закрепляется до incremental Backup API. `.backup.lock` на exact source допускает один backup-процесс. При WAL параллельные записи продолжаются; срок жизни pinned read и запас места на source/output filesystem контролируются. Это не обещание отсутствия любой I/O-нагрузки или роста WAL.

Успех означает: gzip ограничен cap; архив реально распакован в отдельный owned temp; его SHA-256 и размер совпали с raw snapshot; восстановленный SQLite прошёл полный `quick_check` (`quick_check_scope=restored_snapshot`). Второй полный scan исходной копии тех же байтов не требуется. Raw временная копия удаляется перед распаковкой, поэтому одновременно две raw-копии не нужны. Unique архив и manifest публикуются без overwrite, с правами 0600. Только manifest `status=complete` считается принятой копией.

Обычная ошибка чистит только временный каталог своего запуска. Kill/power loss может оставить private temp или gzip без manifest — это не success и не основание автоматически удалить прежние архивы. Прогресс и итог не содержат строк БД или credentials. Архив содержит данные приложения и требует закрытого хранения.

На момент этого backup-выпуска старый `scripts/restore_database.sh` не поддерживал новый gzip-формат и не должен применяться поверх работающей БД. Controlled restore — отдельная операция: остановить всех writers, проверить выбранный архив в отдельном пути, сохранить прежнюю DB/WAL и обеспечить исходный encryption key до переключения. Этот выпуск проверяет реальное восстановление отдельного файла; он не выполняет разрушительное восстановление поверх production и не закрывает полный disaster-recovery drill.

## Проверки

**15 tests passed**: uncheckpointed WAL попадает в копию; commit другого connection после pin не меняет snapshot; восстановленный файл содержит согласованные исходные строки; повтор не меняет прежние архивы; lock не отнимается; capacity/deadline/archive cap/corrupt gzip/digest mismatch отклоняются без success; source не создаётся по неправильному пути; нехватка места на source filesystem также останавливает команду; shell не делает unprotected fallback при остановленном контейнере. Python compilation, shell syntax и `git diff --check` прошли.

Первый production admission честно завершился `insufficient_space` до копирования. Отдельный однократный запуск существующего photo-cache maintenance уменьшил только старые восстанавливаемые JPEG: **32 880 файлов / 3 809 242 240 байт**. Архивы и исходные фотографии не затронуты. Free вырос с **15 518 232 576** до **19 394 699 264** байт. Пороги 10/9 GiB использованы только в этом maintenance-процессе; постоянные настройки web/scheduler не менялись.

Первый прогон от 19:14 UTC остановлен до публикации: на объёме выявлена избыточная стоимость двух полных проверок одинаковых байтов. Финальная команда проверяет восстановленный SQLite и точное совпадение с исходным snapshot; общий deadline — 1800 секунд. Прежние архивы сохранены. Финальный прогон завершён успешно; приёмка 19:39:41 UTC / 22:39 МСК.

## Production acceptance

- Snapshot **2026-09-25 19:27:50 UTC / 22:27:50 МСК**, после startup deployment. Raw **13,638,524,928 байт**, gzip **1,783,746,976 байт**; полный цикл **709.034 секунды**.
- Архив: `/app/data/backups/seller-platform-20260925T193939Z-9d27f378a977.sqlite.gz`; complete manifest рядом. SHA-256 raw/restored: `3a78fa7d815704b5199e002087e37cb3c2cd421d5267a436b222b029c73c2047`. Archive SHA-256: `a877a8b8b91e2813b00fffd9f12eab861b2190f42a3e0f9b0f40cd8c565bd347`. Actual restored file size/hash совпали, `quick_check_scope=restored_snapshot`, `restored_quick_check=ok`.
- Все owned временные файлы финального и остановленного прогонов удалены. Прежние архивы сохранены; свободно **17,613,160,448 байт**. Это новая postdeploy точка восстановления; она не меняет исторический факт использования архива 08:19 UTC при предыдущем deployment.
- После однократного cache maintenance браузер прошёл **7 страниц / 14 layouts**, каталог и шесть категорий, **60/60 фотографий**, 0 route/JS/HTTP/overflow errors и новых provider writes. App image остался `sha256:0632b1e7e6cf3386afe6497cf1119dfb930ae196ced0bf2819c2e74a5656a745`, healthy, 0 restarts.
- Модель выполнения команды подтверждена настоящим Docker invocation из host wrapper; helper hashes сохранены. 15 unit/integration tests, compilation, shell syntax и diff check прошли. Обновление host helper не подменяло app image и не писало startup journal.

Private evidence: `verified-backup-files.json`, `verified-backup-first-attempt.json`, `verified-backup-first.log`, `backup-cache-headroom.json`, `verified-backup-production/`, `verified-backup-release.json`. Первоначальный нехваточный admission и остановленный profiling run не объявлены accepted backups. SIGINT остановил только принадлежащий этой проверке backup-процесс; cleanup завершился, повторное принудительное удаление не потребовалось.

## Открытые пункты эксплуатации

Нет нового расписания, off-host storage или автоматического retention. Прежние архивы сохранены; место для будущих полных copies/rehearsals необходимо рассчитывать заново. Одна очистка восстанавливаемого кэша не решает постоянный рост данных. Encryption key не экспортируется backup-командой: его безопасное отдельное сохранение/восстановление, RPO/RTO, внешнее хранилище и controlled production restore остаются отдельными W8 gates.

Основания и план: [дизайн](../design/verified-sqlite-backups.md), [SQLite Backup API](https://www.sqlite.org/backup.html).


Итоговый Telegram-статус по backup отправлен одной попыткой, доставка **не подтверждена**. Automatic retry не выполнялся; это не меняет результат проверенного backup. Более ранний статус startup deployment был доставлен.


Позднее штатная restore-команда заменена на проверяемое восстановление в новый каталог. Изолированное приложение из принятого архива прошло startup и browser acceptance: [последующий выпуск](2026-09-25-controlled-recovery.md).
