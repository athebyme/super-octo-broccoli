# Восстановление SQLite без перезаписи работающей базы

W8: прежний `scripts/restore_database.sh` делал `cp` поверх основного SQLite-файла до остановки writers. Он не учитывал WAL, не проверял выбранный источник до замены и не сохранял прежний комплект DB/WAL. Исправление этого пути не разрешает агенту восстанавливать production поверх текущих данных.

## План

1. Штатная restore-команда принимает complete manifest проверенного gzip-архива и восстанавливает его только в **новый отдельный каталог**. Существующий target или parent отклоняется до записи; нет остановки/restart/copy поверх live volume и нет обхода через legacy raw `cp`.
2. Один stdlib helper с backup-командой проверяет bounded manifest, допустимое имя архива, размер и checksum. Поддерживается текущий format 1 и явно проверенная legacy release-форма с raw SHA/size, `quick_check=ok`, `round_trip_verified=true`. Bare DB без manifest не выдаётся за подтверждённый источник.
3. Source/archive открываются только для чтения. До начала требуется raw size + reserve. Gzip распаковывается в owned temp, SHA/size сравниваются с manifest, выполняется полный quick_check восстановленного файла. Deadline/reserve действуют на source/output filesystem. Только после успеха публикуются SQLite и receipt; private файлы 0600, новый каталог 0700. Crash без complete receipt не означает готовое восстановление.
4. Проверить corrupt gzip/hash/manifest, traversal, wrong size, неизвестную версию, существующий target/parent, insufficient space/deadline и legacy manifests. Прежняя БД/WAL и архивы должны остаться побайтово неизменными.
5. Применить новую команду к свежему принятому production-архиву в owned isolated directory. Запустить тот же app image с network none, SKIP_SCHEDULER=1 и отключёнными marketplace writes. Проверить normal startup journal, health, сохранность operations/proposals/drafts/accounts и реальные seller-facing read journeys. Приёмка не обращается к провайдеру и не делает production cutover.
6. После терминального завершения изолированного приложения убрать только его owned восстановленную DB/WAL, сохранив accepted archive, receipts и evidence. Зафиксировать измеренное время и незакрытые operational gates.

## Боевой cutover остаётся отдельным решением

До переключения требуется выбранный snapshot/RPO, согласие на потерю изменений после него, остановка **всех** writers, сохранение прежнего DB/WAL и доступность соответствующего encryption key. Старый snapshot не доказывает отсутствие marketplace writes после его времени. Поэтому восстановленное приложение сначала остаётся изолированным от сети с выключенными scheduler/writes; старые pending/attempt=0 jobs нельзя автоматически переигрывать.

Persistent Ozon rate ledger не откатывается вместе с ORM-базой и не считается восстановленным из SQLite-архива. Его сохранение и действующие provider cooldowns, сверка внешних outcomes, key recovery, off-host storage и controlled production cutover требуют отдельной проверки. Проверка рабочей копии не закрывает эти пункты сама по себе.


## Выполнено 25.09

41 tests passed. Restore через новую команду — 382.544 с, штатный startup того же image — 9.071 с до healthy. Проверены analytics 5/36 и catalog 7/14, шесть категорий и 60 первичных фото; schema/FK observations и шесть protected tables неизменны. Внешние изображения для network-none browser предоставлены отдельным fixture и не входят в DB backup. Production не переключался; owned восстановленная база удалена после остановки стенда. [Полный отчёт и границы](../operations/2026-09-25-controlled-recovery.md).
